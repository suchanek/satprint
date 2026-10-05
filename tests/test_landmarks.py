import numpy as np
import pytest
from shapely import Point, box

from satprint.buildings import Building, building_mesh
from satprint.landmarks import MESH_LANDMARKS, _arch, _christ, _eiffel, _needle
from satprint.mesh import Mesh, check_watertight
from satprint.terrain import BBox

# (generator, published width, published height) in meters
SHAPES = {
    "arch": (_arch, 192.0, 192.0),
    "eiffel": (_eiffel, 125.0, 330.0),
    "needle": (_needle, 42.0, 184.0),
    "christ": (_christ, 28.0, 38.0),
}


def _solids(faces: np.ndarray) -> int:
    """Number of separate solids: connected components of the faces' vertices."""
    parent = {int(i): int(i) for i in np.unique(faces)}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b, c in faces.tolist():
        parent[find(a)] = find(b)
        parent[find(b)] = find(c)
    return len({find(i) for i in parent})


@pytest.mark.parametrize("name", SHAPES)
@pytest.mark.parametrize("min_feature_m", [0.0, 6.0, 20.0, 60.0])
def test_generators_are_closed_solids(name, min_feature_m):
    build, _, _ = SHAPES[name]
    v, f = build(min_feature_m)
    mesh = Mesh(v, f)
    assert check_watertight(mesh)["watertight"]
    assert mesh.volume_mm3() > 0
    assert v[:, 2].min() == 0.0


@pytest.mark.parametrize("name", SHAPES)
def test_generators_match_published_size(name):
    build, width, height = SHAPES[name]
    v, _ = build(0.0)
    assert v[:, 2].max() == pytest.approx(height, rel=0.03)
    assert (v[:, 0].max() - v[:, 0].min()) == pytest.approx(width, rel=0.03)


def test_eiffel_has_separate_legs_until_the_archways_close():
    v, f = _eiffel(0.0)
    # the top, the first platform, and four legs below and above it
    assert _solids(f) == 10
    # open between the legs: nothing inside the archway at 20 m
    low = v[:, 2] < 20
    assert not np.any(low & (np.abs(v[:, 0]) < 20) & (np.abs(v[:, 1]) < 20))
    _, f = _eiffel(45.0)
    assert _solids(f) == 1


def test_arch_has_an_open_span_and_a_thick_enough_crown():
    v, f = _arch(0.0)
    assert _solids(f) == 1
    assert v[:, 2].max() == pytest.approx(192.0, rel=0.01)
    # the only vertices near the middle of the span are at the crown
    assert v[np.abs(v[:, 0]) < 5, 2].min() > 150
    for m in (8.0, 12.0):
        v, _ = _arch(m)
        crown = v[np.abs(v[:, 0]) < 1e-6]
        assert crown[:, 1].max() - crown[:, 1].min() >= m - 1e-9
        assert crown[:, 2].max() - crown[:, 2].min() >= 0.8 * m


def test_thin_tops_are_thickened():
    for build in (_eiffel, _needle):
        v, _ = build(8.0)
        top = v[v[:, 2] > v[:, 2].max() - 1e-9]
        assert top[:, 0].max() - top[:, 0].min() >= 8.0 - 1e-9


@pytest.mark.parametrize("lm", MESH_LANDMARKS, ids=lambda lm: lm.name)
def test_apply_landmarks_replaces_buildings_under_a_mesh_landmark(lm):
    from satprint.landmarks import apply_landmarks

    d = 0.01
    bbox = BBox(lm.lat - d, lm.lon - d, lm.lat + d, lm.lon + d)
    under = Building(
        box(lm.lon - 1e-5, lm.lat - 1e-5, lm.lon + 1e-5, lm.lat + 1e-5), 50.0
    )
    far = Building(box(lm.lon + 0.005, lm.lat, lm.lon + 0.0051, lm.lat + 1e-4), 20.0)
    found = apply_landmarks([under, far], bbox)
    assert far in found and under not in found
    (solid,) = [b for b in found if b.solid is not None]
    assert len(found) == 2
    v, f = solid.solid(0.0)
    assert solid.height_m == pytest.approx(v[:, 2].max())
    assert solid.footprint.contains(Point(lm.lon, lm.lat))
    assert check_watertight(Mesh(v, f))["watertight"]


def test_apply_landmarks_leaves_a_landmark_cut_by_the_bbox_alone():
    from satprint.landmarks import apply_landmarks

    lm = MESH_LANDMARKS[2]
    bbox = BBox(lm.lat - 0.01, lm.lon, lm.lat + 0.01, lm.lon + 0.01)
    under = Building(box(lm.lon, lm.lat, lm.lon + 1e-5, lm.lat + 1e-5), 50.0)
    assert apply_landmarks([under], bbox) == [under]


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_mesh_building_on_sloped_relief(scale):
    from satprint.landmarks import apply_landmarks

    lm = MESH_LANDMARKS[2]
    bbox = BBox(lm.lat - 0.0015, lm.lon - 0.0017, lm.lat + 0.0015, lm.lon + 0.0017)
    w, h = bbox.ground_size_m()
    width, depth, mm_per_m = 100.0, 100.0 * h / w, 100.0 / w
    relief = np.tile(np.linspace(0, 10, 20, dtype=np.float32), (20, 1))
    bm = building_mesh(
        apply_landmarks([], bbox),
        bbox,
        relief,
        width,
        depth,
        3.0,
        mm_per_m,
        scale=scale,
    )
    assert bm.count == 1
    mesh = bm.as_mesh()
    assert check_watertight(mesh)["watertight"]
    assert mesh.volume_mm3() > 0
    z = bm.vertices[:, 2]
    # the slope is 5 mm high at the middle of the block, where the Needle stands
    assert z.min() < 3.0 + 5.0
    assert z.max() - z.min() == pytest.approx(184.0 * mm_per_m * scale, rel=1e-3)
    tri = bm.vertices[bm.roof_faces].astype(np.float64)
    nz = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, 2]
    assert len(nz) and (nz > 0).all()
    tri = bm.vertices[bm.wall_faces].astype(np.float64)
    assert (np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, 2] <= 0).all()
