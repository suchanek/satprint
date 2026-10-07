"""Heightmap -> watertight, 3D-printable solid (binary STL), plus a
textured glTF binary (GLB) for viewing and a multi-part 3MF for
multi-material printers.

The model is a rectangular block: a terrain surface on top, four vertical
walls and a flat bottom. Every edge is shared by exactly two triangles with
consistent outward-facing winding, so slicers accept it without repair.

Coordinate system (millimeters):
    X  west -> east      (0 .. width_mm)
    Y  south -> north    (0 .. depth_mm)
    Z  up                (0 at bottom of base)
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from dataclasses import dataclass
from typing import overload

import numpy as np

# Build plates write_3mf can center the model on: name -> (width, depth) mm.
PLATES = {
    "256": (256.0, 256.0),  # Bambu A1, P1, X1
    "180": (180.0, 180.0),  # Bambu A1 mini
}
PLATE_CENTER_MM = (PLATES["256"][0] / 2, PLATES["256"][1] / 2)


def plate_center(plate: str) -> tuple[float, float]:
    """The (x, y) center in mm of the plate named in :data:`PLATES`."""
    width, depth = PLATES[plate]
    return width / 2, depth / 2


@dataclass(frozen=True)
class Mesh:
    """A triangle mesh in millimeters, x east, y north, z up."""

    vertices: np.ndarray  # (n, 3) float32
    faces: np.ndarray  # (m, 3) int64, counter-clockwise seen from outside

    @property
    def triangle_count(self) -> int:
        """Number of triangles."""
        return int(self.faces.shape[0])

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """(min corner, max corner) of the vertices."""
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def volume_mm3(self) -> float:
        """Signed volume via the divergence theorem (positive when outward-wound)."""
        v = self.vertices[self.faces].astype(np.float64)
        a, b, c = v[:, 0], v[:, 1], v[:, 2]
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

    def surface_area_mm2(self) -> float:
        """Total triangle area."""
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
        """A mesh with no solids."""
        return cls(
            np.zeros((0, 3), np.float32),
            np.zeros((0, 3), np.int64),
            np.zeros((0, 3), np.int64),
            0,
        )

    def as_mesh(self) -> Mesh:
        """Roofs and walls as one :class:`Mesh`."""
        return Mesh(self.vertices, np.vstack([self.roof_faces, self.wall_faces]))


def merge_meshes(*meshes: Mesh) -> Mesh:
    """Concatenate meshes into one vertex and face array."""
    offsets = np.cumsum([0] + [m.vertices.shape[0] for m in meshes[:-1]])
    return Mesh(
        vertices=np.vstack([m.vertices for m in meshes]).astype(np.float32),
        faces=np.vstack(
            [m.faces + o for m, o in zip(meshes, offsets, strict=True)]
        ).astype(np.int64),
    )


def merge_building_meshes(*meshes: BuildingMesh) -> BuildingMesh:
    """Concatenate building meshes into one vertex array, solids kept apart."""
    meshes = tuple(m for m in meshes if m.count)
    if not meshes:
        return BuildingMesh.empty()
    offsets = np.cumsum([0] + [m.vertices.shape[0] for m in meshes[:-1]])
    return BuildingMesh(
        vertices=np.vstack([m.vertices for m in meshes]),
        roof_faces=np.vstack(
            [m.roof_faces + o for m, o in zip(meshes, offsets, strict=True)]
        ),
        wall_faces=np.vstack(
            [m.wall_faces + o for m, o in zip(meshes, offsets, strict=True)]
        ),
        count=sum(m.count for m in meshes),
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

    # --- bottom: fan from a center vertex over copies of the perimeter ----
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


def frame_mesh(
    width_mm: float, depth_mm: float, frame_mm: float, height_mm: float
) -> Mesh:
    """A closed rectangular ring around the ``width_mm`` x ``depth_mm`` block.

    The ring's inner walls lie on the block's walls, so the two touch without
    overlapping. It stands from z=0 to ``height_mm``.

    :param width_mm: block width (X).
    :param depth_mm: block depth (Y).
    :param frame_mm: ring width, outward from the block.
    :param height_mm: ring height.
    :return: the ring as one closed, outward-wound solid.
    """
    if frame_mm <= 0 or height_mm <= 0:
        raise ValueError("frame width and height must be positive")
    f = frame_mm
    # outer and inner corners, counter-clockwise from the south-west
    outer = [
        (-f, -f),
        (width_mm + f, -f),
        (width_mm + f, depth_mm + f),
        (-f, depth_mm + f),
    ]
    inner = [(0.0, 0.0), (width_mm, 0.0), (width_mm, depth_mm), (0.0, depth_mm)]
    ring = outer + inner  # 0-3 outer, 4-7 inner
    vertices = np.array(
        [(x, y, height_mm) for x, y in ring] + [(x, y, 0.0) for x, y in ring],
        np.float32,
    )
    lo = 8  # bottom copy of vertex i is i + 8
    faces = []
    for i in range(4):
        j = (i + 1) % 4
        o0, o1, n0, n1 = i, j, 4 + i, 4 + j
        # top: the quad between an outer and an inner edge, counter-clockwise;
        # the bottom is the same quad reversed
        faces += [(o0, o1, n1), (o0, n1, n0)]
        faces += [(o0 + lo, n1 + lo, o1 + lo), (o0 + lo, n0 + lo, n1 + lo)]
        # outer wall faces out; the inner wall faces the block
        faces += [(o0 + lo, o1 + lo, o1), (o0 + lo, o1, o0)]
        faces += [(n1 + lo, n0 + lo, n0), (n1 + lo, n0, n1)]
    return Mesh(vertices=vertices, faces=np.array(faces, np.int64))


def heightmap_split_solids(
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
    cell_mask: np.ndarray,
    skin_mm: float | None = None,
) -> tuple[Mesh, Mesh]:
    """Split the :func:`heightmap_to_mesh` block into two closed solids.

    Each grid cell is a column from the floor to the terrain; the cells where
    ``cell_mask`` is True form the first solid and the rest the second. The
    two meet in vertical walls along the mask boundary, so together they fill
    the same block. ``cell_mask`` must have no 2x2 checkerboards (see
    ``water._fix_diagonals``), or the solids touch along a single edge.

    With ``skin_mm``, the masked solid is only a skin that thick under the
    terrain, and the unmasked solid fills the block beneath it, so the lower
    layers print in one filament. Where the terrain is thinner than twice the
    skin, the skin is half the terrain height.

    :param relief_mm: (rows, cols) heights above the base top, row 0 north.
    :param width_mm: block width.
    :param depth_mm: block depth.
    :param base_mm: base thickness under the lowest terrain point.
    :param cell_mask: (rows-1, cols-1) booleans, one per grid cell.
    :param skin_mm: thickness of the masked solid; None for full columns.
    :return: (masked solid, unmasked solid); either may have no faces.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    if np.min(relief) < 0:  # match heightmap_to_mesh
        relief = relief - np.min(relief)
    rows, cols = relief.shape
    if cell_mask.shape != (rows - 1, cols - 1):
        raise ValueError("cell_mask must be (rows-1, cols-1)")
    xs = np.linspace(0.0, width_mm, cols)
    ys = np.linspace(depth_mm, 0.0, rows)
    gx, gy = np.meshgrid(xs, ys)
    n = rows * cols
    top = np.column_stack([gx.ravel(), gy.ravel(), (relief + base_mm).ravel()])
    bottom = top.copy()
    bottom[:, 2] = 0.0
    # Vertex k is grid node k on the terrain, k + n on the floor and k + low
    # at the bottom of the skin.
    low = 2 * n
    under = top.copy()
    under[:, 2] -= np.minimum(skin_mm or 0.0, top[:, 2] / 2)
    vertices = np.vstack([top, bottom, under])
    key = 3 * n  # edge (u, v) -> u * key + v

    r = np.arange(rows - 1)[:, None]
    c = np.arange(cols - 1)[None, :]
    a = (r * cols + c).ravel()
    b, d = a + 1, a + cols
    e = d + 1
    empty = Mesh(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int64))

    def cell_tops(cells: np.ndarray) -> np.ndarray:
        ca, cb, cd, ce = a[cells], b[cells], d[cells], e[cells]
        return np.concatenate(
            [np.column_stack([ca, cd, ce]), np.column_stack([ca, ce, cb])]
        )

    def edges(tops: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Directed top edges whose reverse is absent, and all edge keys."""
        u = tops.ravel()
        v = np.roll(tops, -1, axis=1).ravel()
        fwd = u * key + v
        edge = ~np.isin(fwd, v * key + u)
        return u[edge], v[edge], fwd

    def wall(lo_u, lo_v, hi_u, hi_v) -> np.ndarray:
        """Quads under top edges hi_u -> hi_v, facing out of the region."""
        return np.concatenate(
            [np.column_stack([lo_u, lo_v, hi_v]), np.column_stack([lo_u, hi_v, hi_u])]
        )

    def mesh(*faces: np.ndarray) -> Mesh:
        f = np.vstack(faces)
        used, remap = np.unique(f, return_inverse=True)
        return Mesh(
            vertices=vertices[used].astype(np.float32),
            faces=remap.reshape(f.shape).astype(np.int64),
        )

    def column(cells: np.ndarray, floor: int) -> Mesh:
        """Cells from the terrain down to the floor (n) or the skin (low)."""
        if not cells.any():
            return empty
        tops = cell_tops(cells)
        u, v, _ = edges(tops)
        return mesh(tops, wall(u + floor, v + floor, u, v), tops[:, ::-1] + floor)

    mask = cell_mask.ravel()
    if not skin_mm:
        return column(mask, n), column(~mask, n)
    if not mask.any():
        return empty, column(~mask, n)

    # The unmasked solid: its own cells to the terrain, the masked cells to
    # the bottom of the skin, both down to the floor. Every outer wall is cut
    # at the skin height so the two kinds of wall meet edge to edge.
    tops = np.vstack([cell_tops(~mask), cell_tops(mask) + low])
    u, v, fwd = edges(tops)
    raised = u < n
    step = raised & np.isin((v + low) * key + u + low, fwd)
    sunk = ~raised & np.isin((v - low) * key + u - low, fwd)
    rim = raised & ~step
    lo_rim = ~raised & ~sunk
    ur, vr, us, vs = u[rim], v[rim], u[lo_rim] - low, v[lo_rim] - low
    base = np.where(tops >= low, tops - low, tops)
    land = mesh(
        tops,
        wall(u[step] + low, v[step] + low, u[step], v[step]),
        wall(ur + n, vr + n, ur + low, vr + low),
        wall(ur + low, vr + low, ur, vr),
        wall(us + n, vs + n, us + low, vs + low),
        base[:, ::-1] + n,
    )
    return column(mask, low), land


def write_3mf(
    parts: list[tuple[str, Mesh, str]],
    name: str = "satprint",
    attribution: str | None = None,
    plate_center: tuple[float, float] = PLATE_CENTER_MM,
) -> bytes:
    """Serialize ``parts`` as one 3MF object made of named, colored parts.

    Slicers (Bambu Studio, OrcaSlicer, PrusaSlicer) open it as one object
    with one part per entry. A Bambu-style ``Metadata/model_settings.config``
    names the parts and gives each distinct color its own filament, numbered
    in order of first use, so parts of one color share a filament. Bambu
    Studio reads it; other slicers ignore it. The colors are display hints
    only. The build item moves the model so its footprint is centered on
    ``plate_center``.

    :param parts: (part name, mesh in mm, ``#RRGGBB`` color); empty meshes
        are skipped.
    :param name: object name.
    :param attribution: data credits, stored as the 3MF ``Copyright``.
    :param plate_center: (x, y) plate center in mm.
    :return: the 3MF file contents.
    """
    from xml.sax.saxutils import escape, quoteattr

    parts = [p for p in parts if p[1].faces.shape[0]]
    if not parts:
        raise ValueError("no parts with faces to write")
    filament = {c: i + 1 for i, c in enumerate(dict.fromkeys(p[2] for p in parts))}
    lo = np.min([p[1].vertices.min(axis=0) for p in parts], axis=0)
    hi = np.max([p[1].vertices.max(axis=0) for p in parts], axis=0)
    dx, dy = np.asarray(plate_center) - (lo[:2] + hi[:2]) / 2
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n',
        f'<metadata name="Title">{escape(name)}</metadata>\n',
        '<metadata name="Application">satprint</metadata>\n',
    ]
    if attribution:
        out.append(f'<metadata name="Copyright">{escape(attribution)}</metadata>\n')
    out.append('<resources>\n<basematerials id="1">\n')
    for part_name, _, color in parts:
        out.append(
            f"<base name={quoteattr(part_name)} displaycolor={quoteattr(color)}/>\n"
        )
    out.append("</basematerials>\n")
    for i, (part_name, mesh, _) in enumerate(parts):
        out.append(
            f'<object id="{i + 2}" type="model" name={quoteattr(part_name)} '
            f'pid="1" pindex="{i}">\n<mesh>\n<vertices>\n'
        )
        v = mesh.vertices.astype(np.float64)
        out.append(
            "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>\n' for x, y, z in v)
        )
        out.append("</vertices>\n<triangles>\n")
        out.append(
            "".join(
                f'<triangle v1="{p}" v2="{q}" v3="{r}"/>\n'
                for p, q, r in mesh.faces.tolist()
            )
        )
        out.append("</triangles>\n</mesh>\n</object>\n")
    parent = len(parts) + 2
    out.append(
        f'<object id="{parent}" type="model" name={quoteattr(name)}>\n<components>\n'
    )
    out.extend(f'<component objectid="{i + 2}"/>\n' for i in range(len(parts)))
    out.append("</components>\n</object>\n</resources>\n")
    out.append(
        f'<build>\n<item objectid="{parent}" '
        f'transform="1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} 0"/>\n</build>\n</model>\n'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" '
            'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        z.writestr("3D/3dmodel.model", "".join(out))
        cfg = [
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n',
            f'  <object id="{parent}">\n'
            f'    <metadata key="name" value={quoteattr(name)}/>\n'
            '    <metadata key="extruder" value="1"/>\n',
        ]
        for i, (part_name, _, color) in enumerate(parts):
            cfg.append(
                f'    <part id="{i + 2}" subtype="normal_part">\n'
                f'      <metadata key="name" value={quoteattr(part_name)}/>\n'
                f'      <metadata key="extruder" value="{filament[color]}"/>\n'
                "    </part>\n"
            )
        cfg.append("  </object>\n</config>\n")
        z.writestr("Metadata/model_settings.config", "".join(cfg))
    return buf.getvalue()


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
    """Serialize ``mesh`` as binary STL. Returns bytes when ``target`` is None."""
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
    """Serialize a :func:`heightmap_to_mesh` block as GLB with ``texture``
    draped over the top surface.

    The top surface is the first ``rows * cols`` vertices and the first
    ``2 * (rows-1) * (cols-1)`` faces, as ``heightmap_to_mesh`` builds them.
    Grid node (r, c) gets texture coordinates (c / (cols-1), r / (rows-1)),
    so the image must cover the same area with row 0 at the north edge. The
    walls and base are a second primitive in a plain material. Output is in
    meters, Y up, as glTF requires.

    :param mesh: block from :func:`heightmap_to_mesh`.
    :param rows: heightmap rows used to build ``mesh``.
    :param cols: heightmap columns used to build ``mesh``.
    :param texture: encoded image bytes (JPEG or PNG).
    :param mime_type: ``image/jpeg`` or ``image/png``.
    :param name: mesh and node name.
    :param copyright: data attribution, stored in ``asset.copyright``.
    :param buildings: optional building solids. Their roofs take the same
        texture, mapped by plan position; walls and floors are plain gray.
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
                # Linear-space #d9c9a8, the color of the STL preview.
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
