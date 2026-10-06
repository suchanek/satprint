import gzip
import json
import math

import mapbox_vector_tile
import numpy as np
import pytest
from shapely import LineString, box

from satprint.bridges import bridge_mesh, bridges_from_vector_tiles
from satprint.mesh import (
    BuildingMesh,
    Mesh,
    check_watertight,
    heightmap_to_mesh,
    merge_building_meshes,
    merge_meshes,
)
from satprint.terrain import BBox
from satprint.water import water_from_vector_tiles

Z, X, Y = 14, 4824, 6157  # midtown Manhattan
WIDTH = 100.0


def _bbox(z, x, y) -> BBox:
    def lon(gx):
        return gx / 2**z * 360.0 - 180.0

    def lat(gy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / 2**z))))

    return BBox(lat(y + 1), lon(x), lat(y), lon(x + 1))


BBOX = _bbox(Z, X, Y)
W_M, H_M = BBOX.ground_size_m()
DEPTH = WIDTH * H_M / W_M
MM_PER_M = WIDTH / W_M
PX = WIDTH / 4096  # model mm per tile pixel

RIVER = box(1800, 0, 2300, 4096)  # 12 mm wide, north to south


def encode(water=(), lines=(), outlines=(), z=Z, x=X, y=Y) -> tuple:
    """A tile with a water layer and a transportation layer."""
    layers = [
        {
            "name": "water",
            "features": [
                {"geometry": g, "properties": {"class": "river"}} for g in water
            ],
        },
        {
            "name": "transportation",
            "features": [
                {
                    "geometry": g,
                    "properties": {"class": cls, "brunnel": "bridge"},
                }
                for g, cls in lines
            ]
            + [{"geometry": g, "properties": {"class": "bridge"}} for g in outlines],
        },
    ]
    data = mapbox_vector_tile.encode(
        layers, default_options={"extents": 4096, "y_coord_down": True}
    )
    return z, x, y, gzip.compress(data)


def relief_with_channel(left=4.0, right=8.0, channel=1.0, rows=41, cols=41):
    """Banks at ``left`` and ``right`` mm, a channel under the river."""
    relief = np.zeros((rows, cols))
    xs = (np.arange(cols) / (cols - 1)) * 4096
    relief[:, xs < 1800] = left
    relief[:, xs > 2300] = right
    relief[:, (xs >= 1800) & (xs <= 2300)] = channel
    return relief


def make(tiles, relief=None, **kw) -> BuildingMesh:
    outlines, lines = bridges_from_vector_tiles(tiles)
    return bridge_mesh(
        outlines,
        lines,
        water_from_vector_tiles(tiles),
        BBOX,
        relief_with_channel() if relief is None else relief,
        WIDTH,
        DEPTH,
        3.0,
        MM_PER_M,
        **kw,
    )


ROAD = LineString([(800, 2048), (3300, 2048)])
LINE_TILE = encode(water=[RIVER], lines=[(ROAD, "primary")])
OUTLINE_TILE = encode(water=[RIVER], outlines=[box(800, 1990, 3300, 2110)])


def solids(bm: BuildingMesh) -> list[np.ndarray]:
    """Vertex indices of each connected solid."""
    parent = list(range(bm.vertices.shape[0]))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for f in np.vstack([bm.roof_faces, bm.wall_faces]):
        parent[find(f[1])] = find(f[0])
        parent[find(f[2])] = find(f[0])
    groups: dict[int, list[int]] = {}
    for i in range(len(parent)):
        groups.setdefault(find(i), []).append(i)
    return [np.array(g) for g in groups.values()]


def test_bridge_features_are_read_from_the_transportation_layer():
    tile = encode(
        water=[RIVER],
        lines=[(ROAD, "primary"), (LineString([(100, 100), (200, 100)]), "path")],
        outlines=[box(800, 1990, 3300, 2110)],
    )
    outlines, lines = bridges_from_vector_tiles([tile])
    assert len(outlines) == 1 and outlines[0].area > 0
    assert sorted(c for _, c in lines) == ["path", "primary"]
    # a plain road is not a bridge
    plain = mapbox_vector_tile.encode(
        [
            {
                "name": "transportation",
                "features": [
                    {"geometry": ROAD, "properties": {"class": "primary"}},
                    {"geometry": box(0, 0, 9, 9), "properties": {"class": "pier"}},
                ],
            }
        ],
        default_options={"extents": 4096, "y_coord_down": True},
    )
    assert bridges_from_vector_tiles([(Z, X, Y, plain)]) == ([], [])


@pytest.mark.parametrize("tile", [LINE_TILE, OUTLINE_TILE], ids=["line", "outline"])
def test_deck_clears_the_water_and_solids_are_closed(tile):
    # banks barely above the channel: the deck has to arch to clear the water
    relief = relief_with_channel(left=1.2, right=1.2, channel=1.0)
    bm = make([tile], relief, pier_spacing_mm=0)
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]
    terrain = heightmap_to_mesh(relief, WIDTH, DEPTH, 3.0)
    assert check_watertight(merge_meshes(terrain, bm.as_mesh()))["watertight"]
    v = bm.vertices
    water = 1.0 + 3.0
    left, deck, right = sorted(solids(bm), key=lambda s: v[s, 0].mean())
    mid = np.abs(v[deck, 0] - 2048 * PX) < 1.0
    assert v[deck][mid, 2].max() >= water + 1.0 - 1e-4  # top clears the water
    assert v[deck][mid, 2].min() >= water + 1.0 - 0.6 - 1e-4  # open underneath
    # the arch rises from the landings, which are flat at the banks
    assert v[deck, 2].max() > v[left, 2].max() + 0.3
    assert v[left, 2].max() == pytest.approx(1.2 + 3.0 + 0.2, abs=1e-3)


def test_landings_reach_below_the_terrain_and_slope_with_the_banks():
    relief = relief_with_channel(left=4.0, right=8.0)
    bm = make([LINE_TILE], relief, pier_spacing_mm=0)
    v = bm.vertices
    parts = solids(bm)
    assert len(parts) == 3  # two landings and one deck
    left, deck, right = sorted(parts, key=lambda s: v[s, 0].mean())
    # flat at the highest ground plus the minimum height, sunk below the terrain
    assert v[left, 2].max() == pytest.approx(4.0 + 3.0 + 0.2, abs=1e-3)
    assert v[left, 2].min() <= 4.0 + 3.0 - 0.3 + 1e-3
    assert v[right, 2].max() == pytest.approx(8.0 + 3.0 + 0.2, abs=1e-3)
    # the deck climbs from the low bank to the high one
    xs, zs = v[deck, 0], v[deck, 2]
    west = zs[xs < xs.min() + 0.5].max()
    east = zs[xs > xs.max() - 0.5].max()
    assert east > west + 2.0


@pytest.mark.parametrize("spacing, piers", [(0, 0), (30.0, 0), (5.0, 2), (4.0, 3)])
def test_pier_count_follows_the_spacing(spacing, piers):
    relief = relief_with_channel()
    bm = make([LINE_TILE], relief, pier_spacing_mm=spacing)
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]
    # a pier reaches below the water; a deck slab does not
    v = bm.vertices
    below = [
        s
        for s in solids(bm)
        if v[s, 0].mean() > 1800 * PX
        and v[s, 0].mean() < 2300 * PX
        and v[s, 2].min() < 4.0
    ]
    assert len(below) == piers


def test_no_bridges_and_dry_bridges_yield_nothing():
    none = make([encode(water=[RIVER])])
    assert none.count == 0 and none.vertices.shape == (0, 3)
    dry = encode(
        water=[RIVER], lines=[(LineString([(100, 500), (1500, 500)]), "primary")]
    )
    assert make([dry]).count == 0
    no_water = encode(lines=[(ROAD, "primary")])
    assert make([no_water]).count == 0


def test_islands_and_missing_banks():
    relief = relief_with_channel(left=4.0, right=8.0)
    # an island under the span is deck, not a third landing
    island = encode(
        water=[RIVER.difference(box(2000, 1950, 2100, 2150))],
        lines=[(ROAD, "primary")],
    )
    bm = make([island], relief, pier_spacing_mm=0)
    assert bm.count == 1 and len(solids(bm)) == 3
    assert check_watertight(bm.as_mesh())["watertight"]
    # water to both edges of the block: no landing, the deck is level and,
    # with nothing else under it, stands on a pier even with piers off
    sea = encode(water=[box(0, 0, 4096, 4096)], lines=[(ROAD, "primary")])
    bm = make([sea], np.full((41, 41), 1.0), pier_spacing_mm=0)
    assert bm.count == 1 and len(solids(bm)) == 3
    top = bm.vertices[np.unique(bm.roof_faces)][:, 2]
    assert top == pytest.approx(1.0 + 3.0 + 1.0, abs=1e-4)  # water + clearance
    assert bm.vertices[:, 2].min() < 1.0 + 3.0


def test_a_short_bridge_with_no_bank_does_not_float():
    # a gangway out to a moored boat: a few meters of path, water at both ends
    gangway = LineString([(2040, 2048), (2056, 2048)])
    tile = encode(water=[RIVER], lines=[(gangway, "path")])
    bm = make([tile])
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]
    v = bm.vertices
    assert v[:, 2].min() < 1.0 + 3.0  # reaches into the water


def test_a_line_split_across_two_tiles_is_one_bridge():
    # a river on the edge between two tiles, a road crossing it
    left = encode(
        water=[box(3700, 0, 4096, 4096)],
        lines=[(LineString([(2900, 2048), (4096, 2048)]), "primary")],
    )
    right = encode(
        water=[box(0, 0, 400, 4096)],
        lines=[(LineString([(0, 2048), (1300, 2048)]), "primary")],
        x=X + 1,
    )
    outlines, lines = bridges_from_vector_tiles([left, right])
    assert len(lines) == 2
    both = BBox(BBOX.south, BBOX.west, BBOX.north, _bbox(Z, X + 1, Y).east)
    bm = bridge_mesh(
        outlines,
        lines,
        water_from_vector_tiles([left, right]),
        both,
        np.zeros((41, 81)),
        2 * WIDTH,
        DEPTH,
        3.0,
        MM_PER_M,
        pier_spacing_mm=0,
    )
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]


def test_bridges_join_the_buildings_mesh():
    bm = make([LINE_TILE], pier_spacing_mm=0)
    other = make([OUTLINE_TILE], pier_spacing_mm=0)
    both = merge_building_meshes(bm, BuildingMesh.empty(), other)
    assert both.count == 2
    assert both.vertices.shape[0] == bm.vertices.shape[0] + other.vertices.shape[0]
    assert check_watertight(both.as_mesh())["watertight"]
    assert merge_building_meshes(BuildingMesh.empty()).count == 0


def _run_cli(monkeypatch, capsys, tmp_path, *extra):
    from satprint import cli
    from satprint.terrain import fetch_terrarium
    from tests.test_app import FakeVectorTiles
    from tests.test_terrain import FakeFetcher

    monkeypatch.setattr(
        cli,
        "fetch_terrarium",
        lambda bbox, target_cols: fetch_terrarium(
            bbox, target_cols=target_cols, fetcher=FakeFetcher()
        ),
    )
    monkeypatch.setattr(cli, "VectorTileClient", FakeVectorTiles)
    out = tmp_path / "bay.stl"
    area = ["40.85", "-73.99", "40.868", "-73.968"]
    argv = ["build", "--bbox", *area, "--resolution", "64", "-o", str(out), *extra]
    assert cli.main(argv) == 0
    out_text = capsys.readouterr().out
    return json.loads(out_text)


def test_cli_bridges_and_piers(monkeypatch, capsys, tmp_path):
    plain = _run_cli(monkeypatch, capsys, tmp_path)
    assert "bridges" not in plain
    info = _run_cli(monkeypatch, capsys, tmp_path, "--bridges")
    assert info["bridges"] >= 1 and info["watertight"] is True
    assert info["triangles"] > plain["triangles"]
    bare = _run_cli(monkeypatch, capsys, tmp_path, "--bridges", "--bridge-piers", "0")
    assert bare["bridges"] == info["bridges"] and bare["watertight"] is True
    assert bare["triangles"] < info["triangles"]


def test_cli_bridges_need_a_bbox_and_a_small_area(capsys):
    from satprint import cli

    assert cli.main(["build", "--synthetic", "--bridges"]) == 2
    assert "--bridges" in capsys.readouterr().err
    big = ["45.0", "7.0", "45.2", "7.3"]  # ~500 km2
    assert cli.main(["build", "--bbox", *big, "--bridges"]) == 2
    assert "limited to 40 km2" in capsys.readouterr().err


def test_a_cell_whose_outline_touches_itself_is_still_built():
    from shapely import Polygon

    from satprint.bridges import _cell_prisms

    # the hole touches the outline at (0, 2), a vertex once densified
    cell = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(0, 2), (2, 1), (2, 3)]])
    prisms = _cell_prisms(cell)
    assert prisms
    for v, roof, walls in prisms:
        assert check_watertight(Mesh(v, np.vstack([roof, walls])))["watertight"]
