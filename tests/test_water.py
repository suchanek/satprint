import io
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pytest
from shapely import Polygon, box

from satprint.mesh import (
    Mesh,
    check_watertight,
    heightmap_split_solids,
    heightmap_to_mesh,
    write_3mf,
)
from satprint.terrain import BBox, Heightmap
from satprint.water import (
    _fix_diagonals,
    level_water,
    water_from_vector_tiles,
    water_mask,
    water_zoom,
)
from tests.test_buildings import encode_tile

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
BBOX = BBox(40.0, -74.0, 40.01, -73.99)


def _checkerboards(m):
    return int(
        (
            (m[:-1, :-1] == m[1:, 1:])
            & (m[:-1, 1:] == m[1:, :-1])
            & (m[:-1, :-1] != m[:-1, 1:])
        ).sum()
    )


@pytest.mark.parametrize("seed", range(5))
def test_fix_diagonals_removes_every_checkerboard(seed):
    raw = np.random.default_rng(seed).random((30, 40)) < 0.4
    fixed = _fix_diagonals(raw)
    assert _checkerboards(fixed) == 0
    assert (fixed | ~raw).all()  # only ever adds water


def test_water_mask_from_polygons_and_flat_sea():
    rows, cols = 21, 21
    relief = np.full((rows, cols), 5.0)
    relief[-3:, :] = 0.0  # a flattened sea strip along the south edge
    west_half = Polygon(
        [(-74.0, 40.0), (-73.995, 40.0), (-73.995, 40.01), (-74.0, 40.01)]
    )
    m = water_mask([west_half], BBOX, relief, 100, 100)
    assert m.shape == (rows - 1, cols - 1)
    assert m[:, :9].all() and not m[:, 11:].any()
    sea = water_mask([], BBOX, relief, 100, 100, sea_level_flat=True)
    assert sea[-2:, :].all() and not sea[:-2, :].any()


def test_water_from_tiles_skips_pools():
    data = encode_tile(
        [
            {"geometry": box(0, 0, 100, 100), "properties": {"class": "river"}},
            {
                "geometry": box(200, 200, 210, 210),
                "properties": {"class": "swimming_pool"},
            },
        ],
        layer="water",
    )
    assert len(water_from_vector_tiles([(14, 4824, 6157, data)])) == 1


def test_water_zoom_keeps_tile_count_small():
    assert water_zoom(BBox(40.70, -74.02, 40.72, -74.00)) == 14
    assert water_zoom(BBox(45.0, 6.0, 46.0, 7.5)) < 12


@pytest.mark.parametrize("seed", range(3))
def test_split_solids_are_closed_and_fill_the_block(seed):
    rng = np.random.default_rng(seed)
    relief = rng.random((25, 35)) * 8
    mask = _fix_diagonals(rng.random((24, 34)) < 0.35)
    wet, dry = heightmap_split_solids(relief, 70, 50, 2, mask)
    assert check_watertight(wet)["watertight"] and check_watertight(dry)["watertight"]
    whole = heightmap_to_mesh(relief, 70, 50, 2).volume_mm3()
    assert wet.volume_mm3() + dry.volume_mm3() == pytest.approx(whole, rel=1e-6)
    assert wet.volume_mm3() > 0 and dry.volume_mm3() > 0


def test_split_with_all_or_nothing_masked():
    relief = np.ones((5, 6))
    wet, dry = heightmap_split_solids(relief, 10, 10, 1, np.ones((4, 5), bool))
    assert dry.faces.shape[0] == 0 and check_watertight(wet)["watertight"]
    with pytest.raises(ValueError):
        heightmap_split_solids(relief, 10, 10, 1, np.ones((5, 6), bool))


def test_3mf_is_one_object_of_named_colored_parts():
    relief = np.random.default_rng(0).random((8, 9))
    mask = np.zeros((7, 8), bool)
    mask[:, :3] = True
    wet, dry = heightmap_split_solids(relief, 40, 30, 2, mask)
    empty = Mesh(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int64))
    data = write_3mf(
        [
            ("land", dry, "#8A9A5B"),
            ("water", wet, "#2F6FB3"),
            ("buildings", empty, "#E8E8E8"),
        ],
        name="bay & harbour",
        attribution="(c) OSM",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= set(
            z.namelist()
        )
        root = ET.fromstring(z.read("3D/3dmodel.model"))
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    assert root.get("unit") == "millimeter"
    meta = {m.get("name"): m.text for m in root.findall("m:metadata", NS)}
    assert meta["Title"] == "bay & harbour" and meta["Copyright"] == "(c) OSM"
    bases = root.findall("m:resources/m:basematerials/m:base", NS)
    assert [(b.get("name"), b.get("displaycolor")) for b in bases] == [
        ("land", "#8A9A5B"),
        ("water", "#2F6FB3"),
    ]  # the empty buildings part is skipped
    objects = root.findall("m:resources/m:object", NS)
    meshes = [o for o in objects if o.find("m:mesh", NS) is not None]
    assert [o.get("name") for o in meshes] == ["land", "water"]
    land = meshes[0].find("m:mesh", NS)
    assert len(land.findall("m:vertices/m:vertex", NS)) == dry.vertices.shape[0]
    assert len(land.findall("m:triangles/m:triangle", NS)) == dry.triangle_count
    (parent,) = [o for o in objects if o.find("m:components", NS) is not None]
    ids = [c.get("objectid") for c in parent.findall("m:components/m:component", NS)]
    assert ids == [o.get("id") for o in meshes]
    (item,) = root.findall("m:build/m:item", NS)
    assert item.get("objectid") == parent.get("id")
    # Bambu Studio part settings: one entry per part, part n on filament n
    (obj,) = cfg.findall("object")
    assert obj.get("id") == parent.get("id")
    got = [
        (p.get("id"), {m.get("key"): m.get("value") for m in p.findall("metadata")})
        for p in obj.findall("part")
    ]
    assert got == [
        (meshes[0].get("id"), {"name": "land", "extruder": "1"}),
        (meshes[1].get("id"), {"name": "water", "extruder": "2"}),
    ]
    with pytest.raises(ValueError):
        write_3mf([("x", empty, "#000000")])


def _lake_heightmap():
    """A 40 m plain over tile (14, 4824, 6157) with a lake reading 90 m."""
    n = 2**14
    x, y = 4824, 6157
    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180

    def lat(row):
        return float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * row / n)))))

    bbox = BBox(lat(y + 1), west, lat(y), east)
    data = np.full((65, 65), 40.0, dtype=np.float32)
    data[20:45, 20:45] = 90.0  # the lake, as the data has it
    return Heightmap(data, 800.0, 800.0, "test", bbox)


def _lake_tile(water_class):
    # the lake is the tile's middle third, a little larger than the 90 m patch
    return [
        (
            14,
            4824,
            6157,
            encode_tile(
                [
                    {
                        "geometry": box(1100, 1100, 3100, 3100),
                        "properties": {"class": water_class},
                    }
                ],
                layer="water",
            ),
        )
    ]


def test_level_water_sets_a_lake_to_its_shore():
    hm = _lake_heightmap()
    out = level_water(hm, _lake_tile("lake"))
    assert out is not hm and hm.data.max() == 90.0  # the input is not modified
    assert out.data[30, 30] == pytest.approx(40.0)
    assert (out.data <= hm.data).all()  # only ever lowered
    assert out.data.max() == pytest.approx(40.0)


def test_level_water_leaves_rivers_and_flat_lakes_alone():
    hm = _lake_heightmap()
    assert level_water(hm, _lake_tile("river")) is hm
    flat = Heightmap(np.full((65, 65), 40.0, np.float32), 800.0, 800.0, "t", hm.bbox)
    assert level_water(flat, _lake_tile("lake")) is flat
    assert (
        level_water(Heightmap(hm.data, 800.0, 800.0, "t"), _lake_tile("lake")).bbox
        is None
    )


def test_level_water_handles_an_outline_finer_than_the_grid():
    """A coast drawn in thousands of tiny steps, as the tiles have it, took
    gigabytes to buffer before the outline was simplified."""
    import time

    from shapely import Polygon

    rng = np.random.default_rng(0)
    t = np.linspace(0, 2 * np.pi, 6000, endpoint=False)
    r = 1000 + rng.integers(-4, 5, t.size)  # jagged at a fraction of a node
    ring = Polygon(np.column_stack([2048 + r * np.cos(t), 2048 + r * np.sin(t)]))
    tile = [
        (
            14,
            4824,
            6157,
            encode_tile(
                [{"geometry": ring, "properties": {"class": "lake"}}], layer="water"
            ),
        )
    ]
    hm = _lake_heightmap()
    start = time.monotonic()
    out = level_water(hm, tile)
    assert time.monotonic() - start < 1
    assert out.data[32, 32] == pytest.approx(40.0)  # the lake's middle
