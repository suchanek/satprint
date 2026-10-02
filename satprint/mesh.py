"""Heightmap -> watertight, 3D-printable solid (binary STL), plus a
textured glTF binary (GLB) for viewing.

The model is a rectangular block: a terrain surface on top, four vertical
walls and a flat bottom. Every edge is shared by exactly two triangles with
consistent outward-facing winding, so slicers accept it without repair.

Coordinate system (millimetres):
    X  west -> east      (0 .. width_mm)
    Y  south -> north    (0 .. depth_mm)
    Z  up                (0 at bottom of base)
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass
from typing import overload

import numpy as np


@dataclass(frozen=True)
class Mesh:
    vertices: np.ndarray  # (n, 3) float32
    faces: np.ndarray  # (m, 3) int64, counter-clockwise seen from outside

    @property
    def triangle_count(self) -> int:
        return int(self.faces.shape[0])

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def volume_mm3(self) -> float:
        """Signed volume via the divergence theorem (positive when outward-wound)."""
        v = self.vertices[self.faces].astype(np.float64)
        a, b, c = v[:, 0], v[:, 1], v[:, 2]
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

    def surface_area_mm2(self) -> float:
        v = self.vertices[self.faces].astype(np.float64)
        n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
        return float(0.5 * np.linalg.norm(n, axis=1).sum())


@dataclass(frozen=True)
class BuildingMesh:
    """Building solids sharing one vertex array (mm, same axes as Mesh)."""

    vertices: np.ndarray  # (n, 3) float32
    roof_faces: np.ndarray  # (k, 3) int64, facing up
    wall_faces: np.ndarray  # (m, 3) int64, walls and floors
    count: int  # number of separate solids

    @classmethod
    def empty(cls) -> BuildingMesh:
        return cls(
            np.zeros((0, 3), np.float32),
            np.zeros((0, 3), np.int64),
            np.zeros((0, 3), np.int64),
            0,
        )

    def as_mesh(self) -> Mesh:
        return Mesh(self.vertices, np.vstack([self.roof_faces, self.wall_faces]))


def merge_meshes(*meshes: Mesh) -> Mesh:
    """Concatenate meshes into one vertex and face array."""
    offsets = np.cumsum([0] + [m.vertices.shape[0] for m in meshes[:-1]])
    return Mesh(
        vertices=np.vstack([m.vertices for m in meshes]).astype(np.float32),
        faces=np.vstack([m.faces + o for m, o in zip(meshes, offsets)]).astype(
            np.int64
        ),
    )


def _perimeter_indices(rows: int, cols: int) -> np.ndarray:
    """Grid-node indices around the boundary, counter-clockwise seen from above.

    Row 0 is the north edge (max Y); the walk starts at the south-west corner
    and goes east, north, west, south.
    """

    def idx(r, c):
        return r * cols + c

    south = [idx(rows - 1, c) for c in range(0, cols - 1)]
    east = [idx(r, cols - 1) for r in range(rows - 1, 0, -1)]
    north = [idx(0, c) for c in range(cols - 1, 0, -1)]
    west = [idx(r, 0) for r in range(0, rows - 1)]
    return np.asarray(south + east + north + west, dtype=np.int64)


def heightmap_to_mesh(
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
) -> Mesh:
    """Build a solid block whose top surface follows ``relief_mm``.

    Parameters
    ----------
    relief_mm : (rows, cols) array of heights **above the base top**, in mm.
        Row 0 is the northern edge, column 0 the western edge.
    width_mm, depth_mm : physical X/Y extent of the block.
    base_mm : thickness of the solid base under the lowest terrain point.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    if relief.ndim != 2 or min(relief.shape) < 2:
        raise ValueError("relief_mm must be a 2-D array with at least 2x2 samples")
    if base_mm <= 0:
        raise ValueError("base_mm must be positive so the model has a solid floor")
    if not np.isfinite(relief).all():
        raise ValueError("relief_mm contains NaN or infinite values")
    if np.min(relief) < 0:
        relief = relief - np.min(relief)

    rows, cols = relief.shape
    xs = np.linspace(0.0, width_mm, cols)
    ys = np.linspace(depth_mm, 0.0, rows)  # row 0 = north = max Y
    gx, gy = np.meshgrid(xs, ys)
    top = np.column_stack([gx.ravel(), gy.ravel(), (relief + base_mm).ravel()])

    # --- top surface: two CCW triangles per grid cell ---------------------
    r = np.arange(rows - 1)[:, None]
    c = np.arange(cols - 1)[None, :]
    a = (r * cols + c).ravel()  # north-west
    b = a + 1  # north-east
    d = a + cols  # south-west
    e = d + 1  # south-east
    top_faces = np.concatenate(
        [np.column_stack([a, d, e]), np.column_stack([a, e, b])], axis=0
    )

    # --- bottom: fan from a centre vertex over copies of the perimeter ----
    perim = _perimeter_indices(rows, cols)
    n_top = top.shape[0]
    n_p = perim.shape[0]
    bottom_ring = top[perim].copy()
    bottom_ring[:, 2] = 0.0
    centre = np.array([[width_mm / 2.0, depth_mm / 2.0, 0.0]])
    ring_idx = n_top + np.arange(n_p)
    centre_idx = n_top + n_p
    nxt = np.roll(ring_idx, -1)
    # Clockwise seen from above -> normal points down (outwards).
    bottom_faces = np.column_stack([np.full(n_p, centre_idx), nxt, ring_idx])

    # --- walls: quad between consecutive perimeter nodes ------------------
    t0, t1 = perim, np.roll(perim, -1)
    b0, b1 = ring_idx, nxt
    wall_faces = np.concatenate(
        [np.column_stack([b0, b1, t1]), np.column_stack([b0, t1, t0])], axis=0
    )

    vertices = np.vstack([top, bottom_ring, centre]).astype(np.float32)
    faces = np.vstack([top_faces, wall_faces, bottom_faces]).astype(np.int64)
    return Mesh(vertices=vertices, faces=faces)


_STL_DTYPE = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


@overload
def write_binary_stl(
    mesh: Mesh, target: None = None, name: str = "satprint"
) -> bytes: ...
@overload
def write_binary_stl(
    mesh: Mesh, target: str | io.IOBase, name: str = "satprint"
) -> None: ...
def write_binary_stl(
    mesh: Mesh, target: str | io.IOBase | None = None, name: str = "satprint"
) -> bytes | None:
    """Serialise ``mesh`` as binary STL. Returns bytes when ``target`` is None."""
    tri = mesh.vertices[mesh.faces].astype(np.float32)
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    normals = (normals / lengths).astype(np.float32)

    records = np.empty(tri.shape[0], dtype=_STL_DTYPE)
    records["normal"] = normals
    records["v"] = tri
    records["attr"] = 0

    header = name.encode("ascii", "replace")[:80].ljust(80, b"\0")
    payload = header + np.uint32(tri.shape[0]).tobytes() + records.tobytes()

    if target is None:
        return payload
    if isinstance(target, (str, bytes)):
        with open(target, "wb") as fh:
            fh.write(payload)
    else:
        target.write(payload)
    return None


# glTF 2.0 constants
_GL_FLOAT = 5126
_GL_UNSIGNED_INT = 5125
_GL_ARRAY_BUFFER = 34962
_GL_ELEMENT_ARRAY_BUFFER = 34963
_GL_LINEAR = 9729
_GL_LINEAR_MIPMAP_LINEAR = 9987
_GL_CLAMP_TO_EDGE = 33071
_GLB_MAGIC = 0x46546C67  # "glTF"
_GLB_JSON = 0x4E4F534A
_GLB_BIN = 0x004E4942


def _to_gltf(xyz: np.ndarray) -> np.ndarray:
    """satprint axes (X east, Y north, Z up) -> glTF axes (Y up, +Z south).

    A proper rotation, so triangle winding is unchanged.
    """
    return np.column_stack([xyz[:, 0], xyz[:, 2], -xyz[:, 1]])


def _vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Area-weighted smooth normals; vertices no face uses get +Z."""
    v = vertices.astype(np.float64)
    fn = np.cross(v[faces[:, 1]] - v[faces[:, 0]], v[faces[:, 2]] - v[faces[:, 0]])
    n = np.zeros_like(v)
    for k in range(3):
        np.add.at(n, faces[:, k], fn)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(length > 0, n / np.where(length > 0, length, 1.0), [0.0, 0.0, 1.0])
    return n


def write_glb(
    mesh: Mesh,
    rows: int,
    cols: int,
    texture: bytes,
    mime_type: str = "image/jpeg",
    name: str = "satprint",
    copyright: str | None = None,
    buildings: BuildingMesh | None = None,
) -> bytes:
    """Serialise a :func:`heightmap_to_mesh` block as GLB with ``texture``
    draped over the top surface.

    The top surface is the first ``rows * cols`` vertices and the first
    ``2 * (rows-1) * (cols-1)`` faces, as ``heightmap_to_mesh`` builds them.
    Grid node (r, c) gets texture coordinates (c / (cols-1), r / (rows-1)),
    so the image must cover the same area with row 0 at the north edge. The
    walls and base are a second primitive in a plain material. Output is in
    metres, Y up, as glTF requires.

    :param mesh: block from :func:`heightmap_to_mesh`.
    :param rows: heightmap rows used to build ``mesh``.
    :param cols: heightmap columns used to build ``mesh``.
    :param texture: encoded image bytes (JPEG or PNG).
    :param mime_type: ``image/jpeg`` or ``image/png``.
    :param name: mesh and node name.
    :param copyright: data attribution, stored in ``asset.copyright``.
    :param buildings: optional building solids. Their roofs take the same
        texture, mapped by plan position; walls and floors are plain grey.
    :return: the GLB file contents.
    """
    n_top = rows * cols
    n_top_faces = 2 * (rows - 1) * (cols - 1)
    if mesh.vertices.shape[0] < n_top or mesh.faces.shape[0] <= n_top_faces:
        raise ValueError("mesh does not match a rows x cols heightmap block")

    positions = (_to_gltf(mesh.vertices.astype(np.float64)) / 1000.0).astype(np.float32)
    top_faces = mesh.faces[:n_top_faces]
    side_faces = mesh.faces[n_top_faces:]
    normals = _to_gltf(_vertex_normals(mesh.vertices[:n_top], top_faces)).astype(
        np.float32
    )
    rr, cc = np.mgrid[0:rows, 0:cols]
    uv = np.column_stack([cc.ravel() / (cols - 1), rr.ravel() / (rows - 1)]).astype(
        np.float32
    )

    blobs: list[bytes] = []
    views: list[dict] = []
    offset = 0

    def add_view(
        data: bytes, target: int | None = None, stride: int | None = None
    ) -> int:
        nonlocal offset
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        if stride is not None:
            view["byteStride"] = stride
        pad = (-len(data)) % 4
        blobs.append(data + b"\0" * pad)
        offset += len(data) + pad
        views.append(view)
        return len(views) - 1

    # Two accessors share the position view, which glTF only allows with an
    # explicit stride.
    pos_view = add_view(positions.tobytes(), _GL_ARRAY_BUFFER, stride=12)
    nrm_view = add_view(normals.tobytes(), _GL_ARRAY_BUFFER, stride=12)
    uv_view = add_view(uv.tobytes(), _GL_ARRAY_BUFFER, stride=8)
    top_view = add_view(top_faces.astype(np.uint32).tobytes(), _GL_ELEMENT_ARRAY_BUFFER)
    side_view = add_view(
        side_faces.astype(np.uint32).tobytes(), _GL_ELEMENT_ARRAY_BUFFER
    )
    img_view = add_view(texture)

    top_pos = positions[:n_top]
    primitives = [
        {
            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
            "indices": 3,
            "material": 0,
        },
        # No NORMAL: viewers must use flat normals, which suits the flat
        # walls and base.
        {"attributes": {"POSITION": 4}, "indices": 5, "material": 1},
    ]
    accessors = [
        {  # 0: top-surface positions (a prefix of the shared vertex buffer)
            "bufferView": pos_view,
            "componentType": _GL_FLOAT,
            "count": n_top,
            "type": "VEC3",
            "min": top_pos.min(axis=0).tolist(),
            "max": top_pos.max(axis=0).tolist(),
        },
        {
            "bufferView": nrm_view,
            "componentType": _GL_FLOAT,
            "count": n_top,
            "type": "VEC3",
        },
        {
            "bufferView": uv_view,
            "componentType": _GL_FLOAT,
            "count": n_top,
            "type": "VEC2",
        },
        {
            "bufferView": top_view,
            "componentType": _GL_UNSIGNED_INT,
            "count": int(top_faces.size),
            "type": "SCALAR",
        },
        {  # 4: all positions, for the walls and base
            "bufferView": pos_view,
            "componentType": _GL_FLOAT,
            "count": int(positions.shape[0]),
            "type": "VEC3",
            "min": positions.min(axis=0).tolist(),
            "max": positions.max(axis=0).tolist(),
        },
        {
            "bufferView": side_view,
            "componentType": _GL_UNSIGNED_INT,
            "count": int(side_faces.size),
            "type": "SCALAR",
        },
    ]

    if buildings is not None and buildings.count:
        width = float(mesh.vertices[:n_top, 0].max())
        depth = float(mesh.vertices[:n_top, 1].max())
        bv = buildings.vertices.astype(np.float64)
        bpos = (_to_gltf(bv) / 1000.0).astype(np.float32)
        buv = np.column_stack([bv[:, 0] / width, (depth - bv[:, 1]) / depth]).astype(
            np.float32
        )
        bpos_view = add_view(bpos.tobytes(), _GL_ARRAY_BUFFER, stride=12)
        buv_view = add_view(buv.tobytes(), _GL_ARRAY_BUFFER, stride=8)
        roof_view = add_view(
            buildings.roof_faces.astype(np.uint32).tobytes(), _GL_ELEMENT_ARRAY_BUFFER
        )
        wall_view = add_view(
            buildings.wall_faces.astype(np.uint32).tobytes(), _GL_ELEMENT_ARRAY_BUFFER
        )
        a = len(accessors)
        accessors += [
            {
                "bufferView": bpos_view,
                "componentType": _GL_FLOAT,
                "count": int(bpos.shape[0]),
                "type": "VEC3",
                "min": bpos.min(axis=0).tolist(),
                "max": bpos.max(axis=0).tolist(),
            },
            {
                "bufferView": buv_view,
                "componentType": _GL_FLOAT,
                "count": int(buv.shape[0]),
                "type": "VEC2",
            },
            {
                "bufferView": roof_view,
                "componentType": _GL_UNSIGNED_INT,
                "count": int(buildings.roof_faces.size),
                "type": "SCALAR",
            },
            {
                "bufferView": wall_view,
                "componentType": _GL_UNSIGNED_INT,
                "count": int(buildings.wall_faces.size),
                "type": "SCALAR",
            },
        ]
        primitives += [
            {
                "attributes": {"POSITION": a, "TEXCOORD_0": a + 1},
                "indices": a + 2,
                "material": 0,
            },
            {"attributes": {"POSITION": a}, "indices": a + 3, "material": 2},
        ]

    asset: dict = {"version": "2.0", "generator": "satprint"}
    if copyright:
        asset["copyright"] = copyright
    gltf = {
        "asset": asset,
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [
            {
                "name": name,
                "primitives": primitives,
            }
        ],
        "materials": [
            {
                "name": "terrain",
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.9,
                },
            },
            {
                "name": "base",
                # Linear-space #d9c9a8, the colour of the STL preview.
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.693, 0.584, 0.392, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.9,
                },
            },
            {
                "name": "building",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.6, 0.6, 0.62, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.8,
                },
            },
        ],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [
            {
                "magFilter": _GL_LINEAR,
                "minFilter": _GL_LINEAR_MIPMAP_LINEAR,
                "wrapS": _GL_CLAMP_TO_EDGE,
                "wrapT": _GL_CLAMP_TO_EDGE,
            }
        ],
        "images": [{"bufferView": img_view, "mimeType": mime_type}],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": offset}],
    }

    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    bin_chunk = b"".join(blobs)
    total = 12 + 8 + len(js) + 8 + len(bin_chunk)
    return (
        struct.pack("<III", _GLB_MAGIC, 2, total)
        + struct.pack("<II", len(js), _GLB_JSON)
        + js
        + struct.pack("<II", len(bin_chunk), _GLB_BIN)
        + bin_chunk
    )


def read_glb(data: bytes) -> tuple[dict, bytes]:
    """Split GLB bytes into its JSON document and binary chunk."""
    magic, version, total = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC or version != 2 or total != len(data):
        raise ValueError("not a glTF 2.0 binary")
    js_len, js_type = struct.unpack_from("<II", data, 12)
    if js_type != _GLB_JSON:
        raise ValueError("first GLB chunk is not JSON")
    doc = json.loads(data[20 : 20 + js_len])
    bin_len, bin_type = struct.unpack_from("<II", data, 20 + js_len)
    if bin_type != _GLB_BIN:
        raise ValueError("second GLB chunk is not BIN")
    start = 28 + js_len
    return doc, data[start : start + bin_len]


def read_binary_stl(data: bytes) -> np.ndarray:
    """Parse binary STL bytes into an (n, 3, 3) float32 triangle array."""
    count = int(np.frombuffer(data[80:84], dtype="<u4")[0])
    records = np.frombuffer(
        data[84 : 84 + count * _STL_DTYPE.itemsize], dtype=_STL_DTYPE
    )
    return records["v"].copy()


def check_watertight(mesh: Mesh) -> dict:
    """Edge-manifold diagnostics: every directed edge should appear exactly once
    and every undirected edge exactly twice."""
    f = mesh.faces
    directed = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    _, counts = np.unique(directed, axis=0, return_counts=True)
    und = np.sort(directed, axis=1)
    _, ucounts = np.unique(und, axis=0, return_counts=True)
    return {
        "directed_edges": int(directed.shape[0]),
        "duplicate_directed_edges": int((counts > 1).sum()),
        "boundary_edges": int((ucounts != 2).sum()),
        "watertight": bool((counts == 1).all() and (ucounts == 2).all()),
    }
