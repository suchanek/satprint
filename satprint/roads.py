"""OpenStreetMap roads -> one thin solid draped over the terrain.

Roads come from the ``transportation`` layer of the OpenFreeMap vector tiles
the buildings and bridges use. Each line is drawn as a strip of a width per
class (:data:`satprint.bridges.BRIDGE_WIDTH_M`), never narrower than a
printable ``min_width_mm``. At print scale a street is a fraction of a nozzle
wide, so most roads are drawn wider than they are, and minor classes are left
out of large areas, where they would cover the land (:data:`ROAD_MIN_SCALE`).

The strips are joined into one footprint, with water and building footprints
cut out, and the footprint is cut along the triangles of the terrain mesh.
Each piece lies on one flat terrain triangle, so its top can sit exactly
``height_mm`` above the terrain and its floor exactly ``sink_mm`` below it.
Pieces share their vertices, and walls stand only on the footprint's outer
edges, as in :func:`satprint.mesh.heightmap_split_solids`, so each connected
road network is a single closed solid. Tunnels are left out; bridges over
water are :mod:`satprint.bridges`' job.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import shapely
from shapely import LineString, Polygon, box
from shapely.geometry.polygon import orient

from .bridges import BRIDGE_WIDTH_M, DEFAULT_WIDTH_M
from .buildings import _polygons, model_projection, vector_tile_features
from .mesh import BuildingMesh
from .terrain import BBox

# Smallest model scale, in mm per meter, at which a class is drawn by the
# "auto" detail level. Streets are a few hundred meters apart at most, so a
# class is shown once its streets print a few strip widths apart.
ROAD_MIN_SCALE = {
    "motorway": 0.0,
    "trunk": 0.0,
    "primary": 0.0,
    "secondary": 0.003,
    "tertiary": 0.008,
    "minor": 0.03,
    "service": 0.1,
    "track": 0.1,
    "path": 0.1,
}
MAJOR_ROADS = {"motorway", "trunk", "primary", "secondary"}
KEY_DECIMALS = 6  # vertices this close (in mm) are one vertex, so neighbors share them
BLOCK_CELLS = 16  # the footprint is cut into blocks this many cells wide first

Detail = Literal["auto", "major", "all"]


def roads_from_vector_tiles(
    tiles: list[tuple[int, int, int, bytes]],
) -> list[tuple[LineString, str]]:
    """Road lines, in lon/lat, from the tiles' ``transportation`` layer.

    Tunnels are left out, and so are classes not in :data:`ROAD_MIN_SCALE`,
    such as rail, ferries and roads under construction.

    :return: (line, OpenMapTiles class) pairs.
    """
    return [
        (line, props["class"])
        for line, props in vector_tile_features(tiles, "transportation", lines=True)
        if props.get("class") in ROAD_MIN_SCALE and props.get("brunnel") != "tunnel"
    ]


def road_classes(detail: Detail, mm_per_m: float) -> set[str]:
    """The classes drawn at ``detail`` for a model at ``mm_per_m``."""
    if detail == "major":
        return set(MAJOR_ROADS)
    if detail == "all":
        return set(ROAD_MIN_SCALE)
    return {cls for cls, s in ROAD_MIN_SCALE.items() if mm_per_m >= s}


def road_mesh(
    lines: list[tuple[LineString, str]],
    water: list[Polygon],
    footprints: list[Polygon],
    bbox: BBox,
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
    mm_per_m: float,
    detail: Detail = "auto",
    height_mm: float = 0.4,
    sink_mm: float = 0.3,
    min_width_mm: float = 0.8,
    simplify_mm: float = 0.05,
    min_area_mm2: float = 0.2,
) -> BuildingMesh:
    """Roads as a thin solid on the terrain block built from ``relief_mm``.

    :param lines: (road line in lon/lat, OpenMapTiles class) pairs.
    :param water: water polygons in lon/lat; roads stop at the water.
    :param footprints: building footprints in lon/lat; roads stop at them.
    :param bbox: area the terrain block covers.
    :param relief_mm: the relief passed to :func:`heightmap_to_mesh`.
    :param width_mm: block width.
    :param depth_mm: block depth.
    :param base_mm: base thickness under the lowest terrain.
    :param mm_per_m: horizontal model scale.
    :param detail: which classes to draw (:func:`road_classes`).
    :param height_mm: height of the road top above the terrain.
    :param sink_mm: depth of the road floor below the terrain.
    :param min_width_mm: narrowest a road is drawn.
    :param simplify_mm: footprint simplification tolerance.
    :param min_area_mm2: connected pieces smaller than this are dropped.
    :return: the roads as one :class:`BuildingMesh`, the tops as roofs;
        ``count`` is the number of connected road networks.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    if np.min(relief) < 0:  # match heightmap_to_mesh
        relief = relief - np.min(relief)
    rows, cols = relief.shape
    to_model = model_projection(bbox, width_mm, depth_mm)
    inset = 0.05  # keep walls off the block's own walls
    frame = box(inset, inset, width_mm - inset, depth_mm - inset)

    keep = road_classes(detail, mm_per_m)
    kept = [(line, cls) for line, cls in lines if cls in keep]
    if not kept:
        return BuildingMesh.empty()
    strips = shapely.buffer(
        np.array([shapely.transform(line, to_model) for line, _ in kept]),
        np.array(
            [
                max(BRIDGE_WIDTH_M.get(cls, DEFAULT_WIDTH_M) * mm_per_m, min_width_mm)
                / 2
                for _, cls in kept
            ]
        ),
        quad_segs=2,
    )
    footprint = shapely.union_all(strips).intersection(frame)
    cut = [shapely.transform(p, to_model) for p in [*water, *footprints]]
    if cut:
        footprint = footprint.difference(shapely.union_all(cut))
    footprint = shapely.make_valid(footprint.simplify(simplify_mm))
    # Shrinking by a hair parts outlines that touch at a point, where four
    # walls would share one edge.
    footprint = footprint.buffer(-1e-4, join_style="mitre")
    parts = [p for p in _polygons(footprint) if p.area >= min_area_mm2]
    if not parts:
        return BuildingMesh.empty()
    footprint = shapely.MultiPolygon(parts)

    # Grid node (r, c) is at (xs[c], ys[r]); cell (r, c) is split along its
    # NW-SE diagonal into the two triangles heightmap_to_mesh draws.
    xs = np.linspace(0.0, width_mm, cols)
    ys = np.linspace(depth_mm, 0.0, rows)
    pieces, planes = _pieces(footprint, xs, ys)
    if not pieces:
        return BuildingMesh.empty()

    index: dict[tuple[float, float], int] = {}
    xyz: list[tuple[float, float, float]] = []
    tops: list[list[int]] = []
    for piece, (r, c, upper) in zip(pieces, planes, strict=True):
        # Triangle corners: NW, SE and SW (lower) or NE (upper).
        corner = (r, c + 1) if upper else (r + 1, c)
        p = np.array(
            [
                [xs[c], ys[r], relief[r, c]],
                [xs[c + 1], ys[r + 1], relief[r + 1, c + 1]],
                [xs[corner[1]], ys[corner[0]], relief[corner]],
            ]
        )
        normal = np.cross(p[1] - p[0], p[2] - p[0])

        def vid(x: float, y: float, p=p, normal=normal) -> int:
            key = (round(x, KEY_DECIMALS), round(y, KEY_DECIMALS))
            i = index.get(key)
            if i is None:
                z = (
                    p[0, 2]
                    - (normal[0] * (x - p[0, 0]) + normal[1] * (y - p[0, 1]))
                    / normal[2]
                )
                i = index[key] = len(xyz)
                xyz.append((x, y, z))
            return i

        for tri in shapely.constrained_delaunay_triangles(piece).geoms:
            pts = np.asarray(tri.exterior.coords)[:3]
            a, b, c3 = pts
            if (b[0] - a[0]) * (c3[1] - a[1]) - (b[1] - a[1]) * (c3[0] - a[0]) <= 0:
                pts = pts[[0, 2, 1]]
            ids = [vid(float(x), float(y)) for x, y in pts]
            if len(set(ids)) == 3:
                tops.append(ids)
    if not tops:
        return BuildingMesh.empty()

    top = np.asarray(xyz)
    top[:, 2] += base_mm + height_mm
    bottom = top.copy()
    bottom[:, 2] -= height_mm + sink_mm
    n = top.shape[0]
    roof = np.asarray(tops, dtype=np.int64)
    # Boundary edges are the directed top edges whose reverse is absent;
    # each gets a wall facing out, as in heightmap_split_solids.
    u = roof.ravel()
    v = np.roll(roof, -1, axis=1).ravel()
    fwd = u * (2 * n) + v
    edge = ~np.isin(fwd, v * (2 * n) + u)
    u, v = u[edge], v[edge]
    walls = np.concatenate(
        [np.column_stack([u + n, v + n, v]), np.column_stack([u + n, v, u])]
    )
    floor = roof[:, ::-1] + n
    return BuildingMesh(
        vertices=np.vstack([top, bottom]).astype(np.float32),
        roof_faces=roof,
        wall_faces=np.vstack([walls, floor]),
        count=len(parts),
    )


def _pieces(
    footprint, xs: np.ndarray, ys: np.ndarray
) -> tuple[list[Polygon], list[tuple[int, int, bool]]]:
    """``footprint`` cut along the terrain triangles.

    :return: (pieces, (row, col, upper triangle) of each piece's triangle).
    """
    rows, cols = len(ys), len(xs)
    shapely.prepare(footprint)
    pieces: list[Polygon] = []
    planes: list[tuple[int, int, bool]] = []
    for r0 in range(0, rows - 1, BLOCK_CELLS):
        r1 = min(r0 + BLOCK_CELLS, rows - 1)
        for c0 in range(0, cols - 1, BLOCK_CELLS):
            c1 = min(c0 + BLOCK_CELLS, cols - 1)
            block = box(xs[c0], ys[r1], xs[c1], ys[r0])
            if not footprint.intersects(block):
                continue
            local = shapely.intersection(footprint, block)
            if local.is_empty:
                continue
            rr, cc = np.mgrid[r0:r1, c0:c1]
            rr, cc = rr.ravel(), cc.ravel()
            nw = np.column_stack([xs[cc], ys[rr]])
            se = np.column_stack([xs[cc + 1], ys[rr + 1]])
            sw = np.column_stack([xs[cc], ys[rr + 1]])
            ne = np.column_stack([xs[cc + 1], ys[rr]])
            lower = shapely.polygons(np.stack([nw, sw, se, nw], axis=1))
            upper = shapely.polygons(np.stack([nw, se, ne, nw], axis=1))
            tris = np.concatenate([lower, upper])
            where = np.concatenate([np.zeros(len(rr), bool), np.ones(len(rr), bool)])
            rr2, cc2 = np.concatenate([rr, rr]), np.concatenate([cc, cc])
            hit = shapely.intersects(local, tris)
            cut = shapely.intersection(local, tris[hit])
            for geom, r, c, up in zip(cut, rr2[hit], cc2[hit], where[hit], strict=True):
                for poly in _polygons(geom):
                    if poly.area > 0:
                        pieces.append(orient(poly, sign=1.0))
                        planes.append((int(r), int(c), bool(up)))
    return pieces, planes
