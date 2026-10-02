import io

import numpy as np
import pytest
from PIL import Image

from satprint.mesh import (
    check_watertight,
    heightmap_to_mesh,
    read_binary_stl,
    read_glb,
    write_binary_stl,
    write_glb,
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


def _accessor(doc, bin_chunk, i, dtype, width):
    acc = doc["accessors"][i]
    view = doc["bufferViews"][acc["bufferView"]]
    start = view["byteOffset"]
    n = acc["count"] * width
    arr = np.frombuffer(bin_chunk, dtype=dtype, count=n, offset=start)
    return arr.reshape(acc["count"], width) if width > 1 else arr


def test_glb_drapes_texture_over_top_surface():
    rows, cols = 6, 9
    rng = np.random.default_rng(3)
    relief = rng.random((rows, cols)) * 5
    mesh = heightmap_to_mesh(relief, 90, 60, 2)
    buf = io.BytesIO()
    Image.new("RGB", (32, 20), "green").save(buf, format="JPEG")
    jpeg = buf.getvalue()

    glb = write_glb(mesh, rows, cols, jpeg, name="peak", copyright="Imagery (c) X")
    assert len(glb) % 4 == 0
    doc, bin_chunk = read_glb(glb)
    assert doc["asset"]["version"] == "2.0"
    assert doc["asset"]["copyright"] == "Imagery (c) X"
    top, sides = doc["meshes"][0]["primitives"]
    assert doc["materials"][top["material"]]["pbrMetallicRoughness"][
        "baseColorTexture"
    ] == {"index": 0}

    # texture bytes are stored verbatim
    view = doc["bufferViews"][doc["images"][0]["bufferView"]]
    start = view["byteOffset"]
    assert bin_chunk[start : start + view["byteLength"]] == jpeg

    # metres, Y up: width along X, height along Y, depth along -Z
    pos = _accessor(doc, bin_chunk, sides["attributes"]["POSITION"], "<f4", 3)
    assert pos[:, 0].max() == pytest.approx(0.090, abs=1e-6)
    assert pos[:, 1].max() == pytest.approx((relief.max() + 2) / 1000, rel=1e-6)
    assert pos[:, 2].min() == pytest.approx(-0.060, abs=1e-6)

    # UVs: north-west node at (0, 0), south-east node at (1, 1)
    uv = _accessor(doc, bin_chunk, top["attributes"]["TEXCOORD_0"], "<f4", 2)
    assert uv.shape == (rows * cols, 2)
    assert tuple(uv[0]) == (0, 0) and tuple(uv[-1]) == (1, 1)
    top_pos = pos[: rows * cols]
    assert top_pos[0, 0] == 0 and top_pos[0, 2] == pytest.approx(-0.060)

    # top normals point up; all faces between the two primitives are kept
    nrm = _accessor(doc, bin_chunk, top["attributes"]["NORMAL"], "<f4", 3)
    assert (nrm[:, 1] > 0).all()
    assert np.linalg.norm(nrm, axis=1) == pytest.approx(1, abs=1e-5)
    top_idx = _accessor(doc, bin_chunk, top["indices"], "<u4", 1)
    side_idx = _accessor(doc, bin_chunk, sides["indices"], "<u4", 1)
    assert top_idx.max() < rows * cols
    assert (top_idx.size + side_idx.size) // 3 == mesh.triangle_count


def test_glb_rejects_mismatched_grid():
    mesh = heightmap_to_mesh(np.zeros((3, 3)), 10, 10, 1)
    with pytest.raises(ValueError):
        write_glb(mesh, 5, 5, b"x")
    with pytest.raises(ValueError):
        read_glb(b"not a glb at all")
