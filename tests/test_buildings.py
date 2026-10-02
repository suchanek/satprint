import gzip

import mapbox_vector_tile
import numpy as np
import pytest
from shapely import box

from satprint.buildings import (
    DEFAULT_HEIGHT_M,
    LEVEL_HEIGHT_M,
    building_mesh,
    buildings_from_osm,
    buildings_from_vector_tiles,
    parse_height,
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


def encode_tile(features, compress=True):
    """An OpenMapTiles-style tile with a ``building`` layer."""
    data = mapbox_vector_tile.encode(
        [{"name": "building", "features": features}],
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
