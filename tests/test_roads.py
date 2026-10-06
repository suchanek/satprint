import gzip

import mapbox_vector_tile
import numpy as np
from shapely import LineString, box

from satprint.buildings import _sample
from satprint.mesh import check_watertight
from satprint.roads import road_classes, road_mesh, roads_from_vector_tiles
from tests.test_bridges import BBOX, DEPTH, MM_PER_M, PX, WIDTH, X, Y, Z


def lonlat(geom):
    """Tile pixels -> lon/lat, for shapes given in tile pixels."""
    import shapely

    def f(c):
        lon = BBOX.west + c[:, 0] / 4096 * (BBOX.east - BBOX.west)
        lat = BBOX.north - c[:, 1] / 4096 * (BBOX.north - BBOX.south)
        return np.column_stack([lon, lat])

    return shapely.transform(geom, f)


def model_xy(px, py):
    """Tile pixels -> model mm (near enough over one tile)."""
    return px * PX, DEPTH - py * PX


def sloped(rows=41, cols=41, rise=10.0):
    """Relief rising ``rise`` mm from west to east, with a bump."""
    c = np.linspace(0, 1, cols)[None, :]
    r = np.linspace(0, 1, rows)[:, None]
    return rise * c + 2.0 * np.sin(6 * r) * c


def build(lines, water=(), footprints=(), relief=None, **kw):
    relief = sloped() if relief is None else relief
    return road_mesh(
        [(lonlat(g), cls) for g, cls in lines],
        [lonlat(p) for p in water],
        [lonlat(p) for p in footprints],
        BBOX,
        relief,
        WIDTH,
        DEPTH,
        3.0,
        MM_PER_M,
        **kw,
    )


def test_roads_from_vector_tiles_keeps_roads_only():
    line = LineString([(0, 100), (4096, 100)])
    feats = [
        {"geometry": line, "properties": {"class": "primary"}},
        {"geometry": line, "properties": {"class": "minor", "brunnel": "bridge"}},
        {"geometry": line, "properties": {"class": "minor", "brunnel": "tunnel"}},
        {"geometry": line, "properties": {"class": "rail"}},
        {"geometry": line, "properties": {"class": "primary_construction"}},
    ]
    data = mapbox_vector_tile.encode(
        [{"name": "transportation", "features": feats}],
        default_options={"extents": 4096, "y_coord_down": True},
    )
    found = roads_from_vector_tiles([(Z, X, Y, gzip.compress(data))])
    assert sorted(cls for _, cls in found) == ["minor", "primary"]


def test_road_classes_by_scale():
    assert road_classes("auto", 0.001) == {"motorway", "trunk", "primary"}
    city = road_classes("auto", 0.04)
    assert "minor" in city and "service" not in city
    assert "path" in road_classes("auto", 0.2)
    assert road_classes("major", 0.2) == {"motorway", "trunk", "primary", "secondary"}
    assert "service" in road_classes("all", 0.001)


def test_road_drapes_over_the_terrain():
    relief = sloped()
    m = build([(LineString([(200, 300), (3900, 3700)]), "primary")], relief=relief)
    assert m.count == 1
    assert check_watertight(m.as_mesh())["watertight"]
    n = m.vertices.shape[0] // 2
    top, bottom = m.vertices[:n].astype(float), m.vertices[n:].astype(float)
    ground = _sample(relief, WIDTH, DEPTH, top[:, :2]) + 3.0
    # Pieces lie on single terrain triangles; on this relief bilinear and
    # triangle heights differ a little inside a cell.
    assert np.allclose(top[:, 2] - ground, 0.4, atol=0.05)
    assert np.allclose(top[:, 2] - bottom[:, 2], 0.7, atol=1e-4)
    assert m.as_mesh().volume_mm3() > 0


def test_road_network_is_watertight_on_rough_terrain():
    rng = np.random.default_rng(1)
    relief = rng.uniform(0, 3, (41, 41))
    lines = []
    for _ in range(25):
        pts = rng.uniform(0, 4096, (4, 2))
        lines.append((LineString(pts), str(rng.choice(["primary", "minor"]))))
    m = build(lines, relief=relief, detail="all")
    assert m.count >= 1
    assert check_watertight(m.as_mesh())["watertight"]


def test_roads_stop_at_water_and_buildings():
    road = LineString([(0, 2048), (4096, 2048)])
    river = box(1800, 0, 2300, 4096)
    house = box(3000, 1900, 3200, 2200)
    m = build([(road, "primary")], water=[river], footprints=[house])
    assert m.count == 3  # the river and the house split the road
    xy = m.vertices[:, :2].astype(float)
    x0, _ = model_xy(1800, 0)
    x1, _ = model_xy(2300, 0)
    assert not ((xy[:, 0] > x0 + 0.01) & (xy[:, 0] < x1 - 0.01)).any()
    hx0, hy1 = model_xy(3000, 1900)
    hx1, hy0 = model_xy(3200, 2200)
    inside = (
        (xy[:, 0] > hx0 + 0.01)
        & (xy[:, 0] < hx1 - 0.01)
        & (xy[:, 1] > hy0 + 0.01)
        & (xy[:, 1] < hy1 - 0.01)
    )
    assert not inside.any()


def test_narrow_roads_are_widened_and_filtered():
    road = LineString([(0, 2048), (4096, 2048)])
    m = build([(road, "minor")], detail="all", min_width_mm=1.0)
    ys = m.vertices[:, 1]
    assert float(ys.max() - ys.min()) > 0.95  # a 7 m street is ~0.3 mm here
    assert build([(road, "minor")], detail="major").count == 0
    assert build([]).count == 0
    # a road wholly under a building is gone
    whole = box(0, 1900, 4096, 2200)
    assert build([(road, "minor")], footprints=[whole], detail="all").count == 0


def test_roads_stay_inside_the_block():
    m = build([(LineString([(-500, 2048), (4600, 2048)]), "primary")])
    assert m.vertices[:, 0].min() >= 0.0 and m.vertices[:, 0].max() <= WIDTH
