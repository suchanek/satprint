"""Heightmap -> watertight, 3D-printable solid (binary STL).

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
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Mesh:
    vertices: np.ndarray  # (n, 3) float32
    faces: np.ndarray     # (m, 3) int64, counter-clockwise seen from outside

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
    if relief.min() < 0:
        relief = relief - relief.min()

    rows, cols = relief.shape
    xs = np.linspace(0.0, width_mm, cols)
    ys = np.linspace(depth_mm, 0.0, rows)  # row 0 = north = max Y
    gx, gy = np.meshgrid(xs, ys)
    top = np.column_stack([gx.ravel(), gy.ravel(), (relief + base_mm).ravel()])

    # --- top surface: two CCW triangles per grid cell ---------------------
    r = np.arange(rows - 1)[:, None]
    c = np.arange(cols - 1)[None, :]
    a = (r * cols + c).ravel()          # north-west
    b = a + 1                           # north-east
    d = a + cols                        # south-west
    e = d + 1                           # south-east
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


_STL_DTYPE = np.dtype(
    [("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")]
)


def write_binary_stl(mesh: Mesh, target: str | io.IOBase | None = None, name: str = "satprint") -> bytes | None:
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


def read_binary_stl(data: bytes) -> np.ndarray:
    """Parse binary STL bytes into an (n, 3, 3) float32 triangle array."""
    count = int(np.frombuffer(data[80:84], dtype="<u4")[0])
    records = np.frombuffer(data[84:84 + count * _STL_DTYPE.itemsize], dtype=_STL_DTYPE)
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
