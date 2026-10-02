import gzip

import mapbox_vector_tile
import numpy as np
import pytest
import shapely
from shapely import box

from satprint.buildings import (
    DEFAULT_HEIGHT_M,
    LEVEL_HEIGHT_M,
    building_mesh,
    buildings_from_osm,
    buildings_from_vector_tiles,
    model_projection,
    parse_height,
    resolve_overlaps,
)
from satprint.mesh import check_watertight, heightmap_to_mesh, merge_meshes
from satprint.terrain import BBox

BBOX = BBox(40.0, -74.0, 40.01, -73.99)


def _geom(*pts):
    return [{"lat": lat, "lon": lon} for lat, lon in pts]


def _square(lat, lon, d=0.001):
    return _geom(
        (lat, lon), (lat, lon + d), (lat + d, lon + d), (lat + d, lon), (lat, lon)
    )


def way(geometry, **tags):
    return {"type": "way", "id": id(geometry), "tags": tags, "geometry": geometry}


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"height": "45"}, 45.0),
        ({"height": "45 m"}, 45.0),
        ({"height": "12.5m"}, 12.5),
        ({"height": "100 ft"}, 30.48),
        ({"height": "30;40"}, 30.0),
        ({"building:levels": "4"}, 4 * LEVEL_HEIGHT_M),
        ({"height": "tall", "building:levels": "2"}, 2 * LEVEL_HEIGHT_M),
        ({}, DEFAULT_HEIGHT_M),
    ],
)
def test_parse_height(tags, expected):
    assert parse_height(tags) == pytest.approx(expected)


def test_ways_relations_and_filters():
    outer_a = _geom((40.002, -73.998), (40.002, -73.996), (40.004, -73.996))
    outer_b = _geom((40.004, -73.996), (40.004, -73.998), (40.002, -73.998))
    hole = _geom(
        (40.0028, -73.9972),
        (40.0028, -73.9968),
        (40.0032, -73.9968),
        (40.0032, -73.9972),
        (40.0028, -73.9972),
    )
    data = {
        "elements": [
            way(_square(40.006, -73.994), building="yes", height="20"),
            way(_square(40.007, -73.993), building="no"),
            way(_square(40.008, -73.992), building="yes", location="underground"),
            way(_geom((40.0, -74.0), (40.001, -74.0)), building="yes"),  # not closed
            {
                "type": "relation",
                "id": 7,
                "tags": {"building": "yes", "type": "multipolygon"},
                # the outer ring is split over two ways, as OSM often does
                "members": [
                    {"type": "way", "role": "outer", "geometry": outer_a},
                    {"type": "way", "role": "outer", "geometry": outer_b},
                    {"type": "way", "role": "inner", "geometry": hole},
                ],
            },
        ]
    }
    found = buildings_from_osm(data)
    assert len(found) == 2
    rel = next(b for b in found if b.height_m == DEFAULT_HEIGHT_M)
    assert len(rel.footprint.interiors) == 1


def test_parts_replace_their_outline():
    outline = way(_square(40.002, -73.998, 0.002), building="yes", height="100")
    part_low = way(_square(40.002, -73.998), **{"building:part": "yes", "height": "30"})
    part_high = way(
        _square(40.003, -73.997), **{"building:part": "yes", "height": "200"}
    )
    lone = way(_square(40.007, -73.993), building="yes", height="10")
    found = buildings_from_osm({"elements": [outline, part_low, part_high, lone]})
    assert sorted(b.height_m for b in found) == [10, 30, 200]
    assert sum(b.is_part for b in found) == 2


def _block(rows=20, cols=20, relief=None):
    w, h = BBOX.ground_size_m()
    width = 100.0
    depth = width * h / w
    relief = np.zeros((rows, cols), np.float32) if relief is None else relief
    return relief, width, depth, width / w


def test_building_solids_are_closed_and_sit_on_terrain():
    data = {
        "elements": [
            way(_square(40.002, -73.998), building="yes", height="50"),
            way(_square(40.006, -73.994), building="yes", **{"building:levels": "3"}),
        ]
    }
    rows, cols = 20, 20
    slope = np.tile(np.linspace(0, 10, cols, dtype=np.float32), (rows, 1))
    relief, width, depth, mm_per_m = _block(rows, cols, slope)
    bm = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m
    )
    assert bm.count == 2
    assert check_watertight(bm.as_mesh())["watertight"]
    assert bm.as_mesh().volume_mm3() > 0
    terrain = heightmap_to_mesh(relief, width, depth, 3.0)
    assert check_watertight(merge_meshes(terrain, bm.as_mesh()))["watertight"]
    # the 50 m building rises 50 m at plan scale above its highest ground
    assert bm.vertices[:, 2].max() > 3.0 + 50 * mm_per_m
    # floors are sunk below the ground; all inside the block's footprint
    assert bm.vertices[:, 2].min() < 3.0 + 10
    assert bm.vertices[:, 0].min() > 0 and bm.vertices[:, 0].max() < width
    assert bm.vertices[:, 1].min() > 0 and bm.vertices[:, 1].max() < depth


def test_scale_and_minimum_height():
    data = {"elements": [way(_square(40.002, -73.998), building="yes", height="10")]}
    relief, width, depth, mm_per_m = _block()
    one = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m
    )
    three = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m, scale=3
    )
    rise = lambda bm: bm.vertices[:, 2].max() - 3.0  # noqa: E731
    assert rise(three) == pytest.approx(3 * rise(one), rel=1e-5)
    tiny = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m * 1e-4
    )
    assert rise(tiny) == pytest.approx(0.2, abs=1e-5)


def test_buildings_are_clipped_to_the_block():
    straddling = way(_square(40.0095, -73.9905, 0.002), building="yes", height="20")
    outside = way(_square(40.05, -73.95), building="yes")
    relief, width, depth, mm_per_m = _block()
    bm = building_mesh(
        buildings_from_osm({"elements": [straddling, outside]}),
        BBOX,
        relief,
        width,
        depth,
        3.0,
        mm_per_m,
    )
    assert bm.count == 1
    assert bm.vertices[:, 0].max() <= width - 0.05 + 1e-4
    assert bm.vertices[:, 1].max() <= depth - 0.05 + 1e-4
    assert check_watertight(bm.as_mesh())["watertight"]


def test_no_buildings_gives_empty_mesh():
    relief, width, depth, mm_per_m = _block()
    bm = building_mesh([], BBOX, relief, width, depth, 3.0, mm_per_m)
    assert bm.count == 0 and bm.vertices.shape == (0, 3)


def encode_tile(features, compress=True, layer="building"):
    """An OpenMapTiles-style tile with one layer, ``building`` by default."""
    data = mapbox_vector_tile.encode(
        [{"name": layer, "features": features}],
        default_options={"extents": 4096, "y_coord_down": True},
    )
    return gzip.compress(data) if compress else data


def test_vector_tile_buildings():
    z, x, y = 14, 4824, 6157  # midtown Manhattan
    tile = encode_tile(
        [
            {
                "geometry": box(100, 100, 300, 200),
                "properties": {"render_height": 50, "render_min_height": 0},
            },
            # an outline whose parts are mapped: skipped
            {
                "geometry": box(400, 400, 800, 800),
                "properties": {"render_height": 444, "hide_3d": True},
            },
            {
                "geometry": box(500, 500, 600, 600),
                "properties": {"render_height": 444, "render_min_height": 330},
            },
            # spills into the tile buffer: clipped to the tile
            {
                "geometry": box(4000, 1000, 4150, 1100),
                "properties": {"render_height": 20},
            },
            {"geometry": box(10, 3000, 60, 3050), "properties": {}},
        ]
    )
    found = buildings_from_vector_tiles([(z, x, y, tile)])
    assert sorted(b.height_m for b in found) == sorted([20, 50, 444, DEFAULT_HEIGHT_M])
    assert [b.is_part for b in found if b.height_m == 444] == [True]

    n = 2**z
    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
    for b in found:
        minx, miny, maxx, maxy = b.footprint.bounds
        assert west - 1e-9 <= minx and maxx <= east + 1e-9
        assert 40.73 < miny < maxy < 40.77
    clipped = next(b for b in found if b.height_m == 20)
    assert clipped.footprint.bounds[2] == pytest.approx(east)
    # tile row 0 is north: the first box sits further north than the last
    first = next(b for b in found if b.height_m == 50)
    last = next(b for b in found if b.height_m == DEFAULT_HEIGHT_M)
    assert first.footprint.centroid.y > last.footprint.centroid.y


def test_vector_tile_without_buildings():
    empty = mapbox_vector_tile.encode([{"name": "water", "features": []}])
    assert buildings_from_vector_tiles([(14, 0, 0, empty)]) == []


def test_resolve_overlaps_keeps_the_tallest_and_removes_overlap():
    import shapely

    low = box(0, 0, 10, 10)
    high = box(5, 5, 15, 15)  # overlaps low by 25
    spire = box(12, 12, 13, 13)  # inside high, taller still
    alone = box(30, 0, 35, 5)
    touching = box(35, 0, 40, 5)  # shares an edge with alone only
    out = resolve_overlaps(
        [(low, 10.0), (high, 20.0), (spire, 50.0), (alone, 7.0), (touching, 9.0)]
    )
    polys = [p for p, _ in out]
    union = shapely.union_all([low, high, alone, touching])
    assert sum(p.area for p in polys) == pytest.approx(union.area)
    for i, p in enumerate(polys):
        for q in polys[i + 1 :]:
            assert p.intersection(q).area < 1e-9

    def height_at(x, y):
        from shapely import Point

        (h,) = [h for p, h in out if p.contains(Point(x, y))]
        return h

    assert height_at(2, 2) == 10 and height_at(7, 7) == 20 and height_at(14, 6) == 20
    assert height_at(12.5, 12.5) == 50
    assert (alone, 7.0) in out and (touching, 9.0) in out  # untouched


def test_overlapping_buildings_mesh_without_overlap():
    a = way(_square(40.002, -73.998, 0.002), building="yes", height="30")
    b = way(_square(40.003, -73.997, 0.002), building="yes", height="60")
    relief, width, depth, mm_per_m = _block()
    bm = building_mesh(
        buildings_from_osm({"elements": [a, b]}),
        BBOX,
        relief,
        width,
        depth,
        3.0,
        mm_per_m,
    )
    assert check_watertight(bm.as_mesh())["watertight"]
    # three pieces: a's own part, the shared part at 60 m, and b's own part,
    # merged by height into two solids that only touch
    assert bm.count == 2
    assert bm.as_mesh().volume_mm3() > 0


# ---------------------------------------------------------------------------
# Roof shapes
# ---------------------------------------------------------------------------


def _octagon(lat, lon, r=0.0005):
    import math

    pts = [
        (
            lat + r * math.sin(2 * math.pi * i / 8),
            lon + r * math.cos(2 * math.pi * i / 8),
        )
        for i in range(8)
    ]
    return _geom(*pts, pts[0])


def test_parse_roof():
    from shapely import Polygon

    from satprint.buildings import ROOF_PROFILES, parse_roof, radius_m

    sq = Polygon([(0, 40), (0.001, 40), (0.001, 40.001), (0, 40.001)])
    assert parse_roof({"roof:shape": "dome", "roof:height": "12 m"}, 50, sq) == (
        ROOF_PROFILES["dome"],
        12.0,
    )
    assert parse_roof({"roof:shape": "cone", "roof:levels": "2"}, 50, sq)[1] == 6.0
    # untagged height: the footprint's radius, a hemisphere for a dome
    assert parse_roof({"roof:shape": "dome"}, 500, sq)[1] == pytest.approx(radius_m(sq))
    assert parse_roof({"roof:shape": "dome", "roof:height": "80"}, 50, sq)[1] == 50
    assert parse_roof({"roof:shape": "gabled"}, 50, sq) == ((), 0.0)
    assert parse_roof({}, 50, sq) == ((), 0.0)
    for profile in ROOF_PROFILES.values():
        assert profile[-1] == (0.0, 1.0) or profile[-1][0] < 1e-9


@pytest.mark.parametrize("shape", ["dome", "onion", "cone", "pyramidal"])
def test_shaped_roofs_are_closed_and_peak_at_the_top(shape):
    data = {
        "elements": [
            way(
                _octagon(40.005, -73.995),
                building="yes",
                height="60",
                **{"roof:shape": shape, "roof:height": "30"},
            )
        ]
    }
    rows, cols = 20, 20
    slope = np.tile(np.linspace(0, 4, cols, dtype=np.float32), (rows, 1))
    relief, width, depth, mm_per_m = _block(rows, cols, slope)
    bm = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m
    )
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]
    assert bm.as_mesh().volume_mm3() > 0
    terrain = heightmap_to_mesh(relief, width, depth, 3.0)
    assert check_watertight(merge_meshes(terrain, bm.as_mesh()))["watertight"]
    # one apex vertex, 30 m above the eave
    z = bm.vertices[:, 2]
    assert (z == z.max()).sum() == 1
    assert np.isclose(z, z.max() - 30 * mm_per_m, atol=1e-3).sum() == 8
    # the roof faces carry the shape: more than a flat octagon's 6 triangles
    assert bm.roof_faces.shape[0] > 6


def test_shaped_roof_wins_over_overlapping_flat_buildings():
    dome = way(
        _octagon(40.005, -73.995),
        building="yes",
        height="40",
        **{"roof:shape": "dome"},
    )
    # a wide flat podium under the dome, and a lower dome cut by the first
    podium = way(_square(40.0040, -73.9965, 0.003), building="yes", height="10")
    cut = way(
        _octagon(40.0053, -73.9947),
        building="yes",
        height="20",
        **{"roof:shape": "dome"},
    )
    relief, width, depth, mm_per_m = _block()
    found = buildings_from_osm({"elements": [dome, podium, cut]})
    bm = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    assert check_watertight(bm.as_mesh())["watertight"]
    # the podium and the cut dome lose the dome's area, so the volume is
    # the sum of solids that do not overlap
    alone = [
        building_mesh([b], BBOX, relief, width, depth, 3.0, mm_per_m) for b in found
    ]
    assert bm.as_mesh().volume_mm3() < sum(a.as_mesh().volume_mm3() for a in alone)
    # only the tall dome keeps a shaped roof: a single apex
    z = bm.vertices[:, 2]
    assert (z == z.max()).sum() == 1


def test_non_star_footprint_falls_back_to_flat():
    ell = _geom(
        (40.004, -73.996),
        (40.004, -73.993),
        (40.0045, -73.993),
        (40.0045, -73.9955),
        (40.007, -73.9955),
        (40.007, -73.996),
        (40.004, -73.996),
    )
    data = {
        "elements": [
            way(ell, building="yes", height="30", **{"roof:shape": "pyramidal"})
        ]
    }
    relief, width, depth, mm_per_m = _block()
    bm = building_mesh(
        buildings_from_osm(data), BBOX, relief, width, depth, 3.0, mm_per_m
    )
    assert check_watertight(bm.as_mesh())["watertight"]
    # flat at the full height
    top = bm.vertices[:, 2].max()
    assert (bm.vertices[:, 2] == top).sum() == 6
    assert top == pytest.approx(3.0 + 30 * mm_per_m, rel=1e-4)


def test_apply_shapes_replaces_copies_only():
    from satprint.buildings import ROOF_PROFILES, Building, apply_shapes

    plain_copy = Building(box(0, 0, 1, 1), 30)
    neighbor = Building(box(2, 0, 3, 1), 10)
    big = Building(box(-5, -5, 5, 5), 5)  # contains the dome but much larger
    dome = Building(box(0.05, 0.05, 0.95, 0.95), 30, profile=ROOF_PROFILES["dome"])
    out = apply_shapes([plain_copy, neighbor, big], [dome])
    assert out == [neighbor, big, dome]
    assert apply_shapes([neighbor], [Building(box(0, 0, 1, 1), 5)]) == [neighbor]


# ---------------------------------------------------------------------------
# Landmarks
# ---------------------------------------------------------------------------


def test_sphere_landmark_is_a_cut_sphere():
    from satprint.buildings import radius_m
    from satprint.landmarks import apply_landmarks

    sphere_way = way(
        _octagon(40.005, -73.995, 0.0008),
        building="commercial",
        height="112",
        wikidata="Q60749353",
        **{"roof:shape": "dome"},
    )
    other = way(_square(40.002, -73.998), building="yes", height="20")
    found = apply_landmarks(buildings_from_osm({"elements": [sphere_way, other]}))
    sphere = next(b for b in found if b.wikidata == "Q60749353")
    assert sphere.height_m == 112 and sphere.roof_height_m == 112
    # ground circle of a 78.5 m sphere whose center is 33.5 m up
    assert radius_m(sphere.footprint) == pytest.approx(71.0, abs=0.2)
    widest = max(s for s, _ in sphere.profile)
    assert widest * 71.0 == pytest.approx(78.5, abs=0.2)
    assert sphere.profile[-1][0] < 1e-9 and sphere.profile[-1][1] == pytest.approx(1)
    assert sum(b.height_m == 20 for b in found) == 1

    rows, cols = 20, 20
    relief, width, depth, mm_per_m = _block(rows, cols)
    bm = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    assert bm.count == 2
    assert check_watertight(bm.as_mesh())["watertight"]
    assert bm.vertices[:, 2].max() == pytest.approx(3.0 + 112 * mm_per_m, rel=1e-3)


def test_landmark_matches_by_osm_id():
    from satprint.buildings import Building
    from satprint.landmarks import apply_landmarks

    b = Building(box(-115.163, 36.121, -115.161, 36.123), 100, osm_id="way/976405284")
    (sphere,) = apply_landmarks([b])
    assert sphere.profile and sphere.height_m == 112
    plain = Building(box(0, 0, 1, 1), 10, osm_id="way/1")
    assert apply_landmarks([plain]) == [plain]


def test_dome_in_a_taller_wing_is_not_a_pit():
    wing = way(_square(40.0040, -73.9965, 0.003), building="yes", height="30")
    # a dome whose top is below the wing's roof, and one that rises above it
    sunk = way(
        _octagon(40.0050, -73.9955, 0.0002),
        building="yes",
        height="25",
        **{"roof:shape": "dome", "roof:height": "15"},
    )
    proud = way(
        _octagon(40.0058, -73.9943, 0.0002),
        building="yes",
        height="45",
        **{"roof:shape": "dome", "roof:height": "25"},
    )
    relief, width, depth, mm_per_m = _block()
    found = buildings_from_osm({"elements": [wing, sunk, proud]})
    bm = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    assert check_watertight(bm.as_mesh())["watertight"]
    wing_top = 3.0 + 30 * mm_per_m
    # nothing lower than the wing's roof inside the wing: no pit for the
    # sunk dome, and the proud dome starts at the wing's roof
    v = bm.vertices
    to_model = model_projection(BBOX, width, depth)
    wing_model = shapely.transform(found[0].footprint, to_model)
    inside = shapely.contains_xy(wing_model.buffer(-0.01), v[:, 0], v[:, 1])
    tops = v[inside & (v[:, 2] > 3.0 + 1e-6)]
    assert tops[:, 2].min() == pytest.approx(wing_top, abs=1e-4)
    assert v[:, 2].max() == pytest.approx(3.0 + 45 * mm_per_m, rel=1e-4)
    assert (v[:, 2] == v[:, 2].max()).sum() == 1


def test_demoted_dome_is_still_printed():
    big = way(
        _octagon(40.005, -73.995),
        building="yes",
        height="60",
        **{"roof:shape": "dome", "roof:height": "30"},
    )
    small = way(
        _octagon(40.0054, -73.9946, 0.0003),
        building="yes",
        height="20",
        **{"roof:shape": "dome", "roof:height": "10"},
    )
    relief, width, depth, mm_per_m = _block()
    found = buildings_from_osm({"elements": [big, small]})
    both = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    only_big = building_mesh(found[:1], BBOX, relief, width, depth, 3.0, mm_per_m)
    assert check_watertight(both.as_mesh())["watertight"]
    # the small dome's part outside the big one stays, flat
    assert both.count == 2
    assert both.as_mesh().volume_mm3() > only_big.as_mesh().volume_mm3()


def test_lantern_stands_in_a_hole_in_the_dome():
    dome = way(
        _octagon(40.005, -73.995),
        **{
            "building:part": "yes",
            "height": "60",
            "roof:shape": "dome",
            "roof:height": "20",
        },
    )
    lantern = way(
        _octagon(40.005, -73.995, 0.0001),
        **{"building:part": "yes", "height": "75"},
    )
    cupola = way(  # a small dome on the lantern, taller than the big one
        _octagon(40.005, -73.995, 0.00005),
        **{"building:part": "yes", "height": "85", "roof:shape": "dome"},
    )
    relief, width, depth, mm_per_m = _block()
    found = buildings_from_osm({"elements": [dome, lantern, cupola]})
    bm = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    assert bm.count == 3
    assert check_watertight(bm.as_mesh())["watertight"]
    z = bm.vertices[:, 2]
    # the cupola keeps its dome: a single apex at 85 m
    assert z.max() == pytest.approx(3.0 + 85 * mm_per_m, rel=1e-4)
    assert (z == z.max()).sum() == 1  # the main dome is cut, no apex
    # the main dome is still shaped below its cut: rings between its eave
    # and the lantern's top
    eave, top = 3.0 + 40 * mm_per_m, 3.0 + 60 * mm_per_m
    assert len(np.unique(np.round(z[(z > eave + 1e-3) & (z < top)], 4))) > 5
    # the solids fill the hole without passing through each other
    alone = [
        building_mesh([b], BBOX, relief, width, depth, 3.0, mm_per_m) for b in found[:2]
    ]
    v = bm.as_mesh().volume_mm3()
    assert alone[0].as_mesh().volume_mm3() < v
    assert v < sum(a.as_mesh().volume_mm3() for a in alone)


def test_lantern_below_the_cut_is_left_inside_the_dome():
    dome = way(
        _octagon(40.005, -73.995),
        **{
            "building:part": "yes",
            "height": "60",
            "roof:shape": "dome",
            "roof:height": "20",
        },
    )
    stub = way(
        _octagon(40.005, -73.995, 0.0001),
        **{"building:part": "yes", "height": "45"},  # above the eave only
    )
    relief, width, depth, mm_per_m = _block()
    found = buildings_from_osm({"elements": [dome, stub]})
    bm = building_mesh(found, BBOX, relief, width, depth, 3.0, mm_per_m)
    assert bm.count == 1
    assert check_watertight(bm.as_mesh())["watertight"]
    z = bm.vertices[:, 2]
    assert (z == z.max()).sum() == 1
