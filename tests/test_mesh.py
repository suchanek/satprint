import numpy as np
import pytest

from satprint.mesh import (
    check_watertight,
    heightmap_to_mesh,
    read_binary_stl,
    write_binary_stl,
)


@pytest.mark.parametrize("shape", [(2, 2), (3, 5), (40, 25)])
def test_mesh_is_watertight(shape):
    rng = np.random.default_rng(1)
    relief = rng.random(shape) * 7
    mesh = heightmap_to_mesh(relief, 80, 50, 2)
    diag = check_watertight(mesh)
    assert diag["watertight"], diag
    rows, cols = shape
    perimeter = 2 * (rows + cols) - 4
    assert mesh.triangle_count == 2 * (rows - 1) * (cols - 1) + 3 * perimeter


def test_dimensions_and_volume():
    relief = np.zeros((10, 20))
    mesh = heightmap_to_mesh(relief, 100, 40, 5)
    lo, hi = mesh.bounds()
    assert np.allclose(lo, [0, 0, 0]) and np.allclose(hi, [100, 40, 5])
    assert mesh.volume_mm3() == pytest.approx(100 * 40 * 5)
    assert mesh.surface_area_mm2() == pytest.approx(2 * (100 * 40 + 100 * 5 + 40 * 5))


def test_outward_normals_positive_volume_with_relief():
    y, x = np.mgrid[0:30, 0:30]
    relief = 10 * np.exp(-((x - 15) ** 2 + (y - 15) ** 2) / 50)
    mesh = heightmap_to_mesh(relief, 60, 60, 3)
    assert mesh.volume_mm3() > 60 * 60 * 3


def test_stl_roundtrip(tmp_path):
    mesh = heightmap_to_mesh(np.random.rand(6, 7), 30, 20, 1)
    data = write_binary_stl(mesh, name="unit test")
    assert data[:9] == b"unit test"
    tri = read_binary_stl(data)
    assert tri.shape == (mesh.triangle_count, 3, 3)
    assert np.allclose(tri, mesh.vertices[mesh.faces])
    path = tmp_path / "m.stl"
    write_binary_stl(mesh, str(path), name="unit test")
    assert path.read_bytes() == data


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        heightmap_to_mesh(np.zeros((1, 5)), 10, 10, 1)
    with pytest.raises(ValueError):
        heightmap_to_mesh(np.zeros((5, 5)), 10, 10, 0)
    with pytest.raises(ValueError):
        heightmap_to_mesh(np.full((5, 5), np.nan), 10, 10, 1)
