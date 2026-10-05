"""OpenStreetMap bridges over water -> closed solids: a raised deck.

OpenStreetMap has no deck heights, so they are estimated from the banks. The
part of a bridge over water becomes a deck whose height blends the heights of
the landings on its banks, lifted where needed to clear the water, and the
deck is held up by thin piers at even spacing. A short landing on each bank
is flat at the highest ground under it. Land approaches beyond the landing
are left out, and so are bridges that cross no water.

Both pieces come from the ``transportation`` layer of the OpenFreeMap vector
tiles the buildings use: ``man_made=bridge`` outlines (class ``bridge``) and
road, rail and path lines with ``brunnel`` ``bridge``, drawn as strips of a
width per class (:data:`BRIDGE_WIDTH_M`).

Every solid is closed and outward-wound on its own and touches its
neighbors only along shared vertical faces, as in :mod:`satprint.buildings`.
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from shapely import LineString, Polygon, affinity, box
from shapely.ops import unary_union

from .buildings import (
    _polygons,
    _prism,
    _sample,
    model_projection,
    vector_tile_features,
)
from .mesh import BuildingMesh
from .terrain import BBox

# Deck widths in meters by OpenMapTiles class; lines have no width of their own.
BRIDGE_WIDTH_M = {
    "motorway": 12.0,
    "trunk": 11.0,
    "primary": 10.0,
    "secondary": 9.0,
    "tertiary": 8.0,
    "minor": 7.0,
    "service": 5.0,
    "rail": 5.0,
    "transit": 5.0,
    "path": 3.0,
}
DEFAULT_WIDTH_M = 6.0
DENSIFY_MM = 1.0  # cell outlines get a vertex at least this often


def bridges_from_vector_tiles(
    tiles: list[tuple[int, int, int, bytes]],
) -> tuple[list[Polygon], list[tuple[LineString, str]]]:
    """Bridge outlines and lines, in lon/lat, from the tiles' ``transportation``
    layer.

    :return: (outline polygons, (line, class) pairs).
    """
    outlines = [
        poly
        for poly, props in vector_tile_features(tiles, "transportation")
        if props.get("class") == "bridge"
    ]
    lines = [
        (line, props.get("class", ""))
        for line, props in vector_tile_features(tiles, "transportation", lines=True)
        if props.get("brunnel") == "bridge"
    ]
    return outlines, lines


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _beyond(poly: Polygon, u: np.ndarray, lo: float, hi: float) -> bool:
    """True when ``poly`` reaches past ``lo`` or ``hi`` along the direction ``u``."""
    t = shapely.get_coordinates(poly) @ u
    return bool(t.min() < lo or t.max() > hi)


def _deck_top(
    xy: np.ndarray,
    lands: list[Polygon],
    tops: list[float],
    level: float,
    clearance_mm: float,
    ease_mm: float,
) -> np.ndarray:
    """Deck top height at model coordinates ``xy`` over the water.

    The inverse-distance blend of the landing ``tops``, plus the lift to
    ``clearance_mm`` over ``level``, eased in from the banks.
    """
    if not lands:
        return np.full(len(xy), level + clearance_mm)
    pts = shapely.points(xy)
    dist = np.array([shapely.distance(pts, p) for p in lands])
    weight = 1.0 / np.maximum(dist, 1e-9)
    blend = np.array(tops) @ weight / weight.sum(axis=0)
    ease = _smoothstep(dist.min(axis=0) / ease_mm)
    return blend + ease * np.maximum(0.0, level + clearance_mm - blend)


def _cell_prisms(cell: Polygon) -> list:
    """Flat prisms over ``cell``, its outline densified, for the caller to set
    the heights of. An outline that touches itself does not close as it is,
    so it is shrunk by a hair, which parts it where it touches.
    """
    dense = shapely.segmentize(cell, DENSIFY_MM)
    prism = _prism(dense, 0.0, 0.0) if isinstance(dense, Polygon) else None
    if prism is not None:
        return [prism]
    out = []
    for p in _polygons(shapely.make_valid(cell).buffer(-0.01, join_style="mitre")):
        dense = shapely.segmentize(p, DENSIFY_MM)
        prism = _prism(dense, 0.0, 0.0) if isinstance(dense, Polygon) else None
        if prism is not None:
            out.append(prism)
    return out


def bridge_mesh(
    outlines: list[Polygon],
    lines: list[tuple[LineString, str]],
    water: list[Polygon],
    bbox: BBox,
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
    mm_per_m: float,
    sink_mm: float = 0.3,
    min_width_mm: float = 0.8,
    min_height_mm: float = 0.2,
    clearance_mm: float = 1.0,
    deck_mm: float = 0.6,
    pier_spacing_mm: float = 20.0,
    pier_mm: float = 1.0,
    landing_m: float = 30.0,
    min_landing_mm: float = 1.0,
    ease_mm: float = 3.0,
    min_area_mm2: float = 0.2,
) -> BuildingMesh:
    """Bridges over ``water`` as solids on the terrain block built from
    ``relief_mm``.

    The footprint is the union of the outlines and the lines, each buffered
    to its class width. Connected parts that cross water are cut to the
    water plus a short landing on each bank. On each bank piece the deck is
    flat at the highest ground under it plus ``min_height_mm``. Over the
    water its height blends those by distance to each bank piece, and is
    lifted to ``clearance_mm`` above the water, the lift easing in over
    ``ease_mm`` from the banks. The span is cut across the bridge's long
    axis into thin piers, from below the ground up to the deck, and deck
    slabs between them, open underneath. Land between stretches of water,
    such as a tower's island, is part of the span, not a landing. With one
    bank the deck is flat there, with none it is at the water plus the
    clearance.

    :param outlines: bridge outline polygons in lon/lat.
    :param lines: (bridge line in lon/lat, OpenMapTiles class) pairs.
    :param water: water polygons in lon/lat.
    :param bbox: area the terrain block covers.
    :param relief_mm: the relief passed to :func:`heightmap_to_mesh`.
    :param width_mm: block width.
    :param depth_mm: block depth.
    :param base_mm: base thickness under the lowest terrain.
    :param mm_per_m: horizontal model scale.
    :param sink_mm: how far landings and piers go below the terrain.
    :param min_width_mm: narrowest a line's deck is drawn.
    :param min_height_mm: height of a landing above the highest ground under it.
    :param clearance_mm: least height of the deck top above the water.
    :param deck_mm: thickness of a deck slab.
    :param pier_spacing_mm: longest distance between piers; 0 for no piers.
    :param pier_mm: pier thickness along the bridge.
    :param landing_m: how far the bridge continues onto each bank.
    :param min_landing_mm: shortest landing, so small models keep one.
    :param ease_mm: distance from a bank over which the lift reaches full.
    :param min_area_mm2: pieces smaller than this are dropped, and a bridge
        must cross at least this much water.
    :return: all bridges as one :class:`BuildingMesh`; ``count`` is the
        number of bridges.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    if np.min(relief) < 0:  # match heightmap_to_mesh
        relief = relief - np.min(relief)
    to_model = model_projection(bbox, width_mm, depth_mm)
    inset = 0.05  # keep walls off the block's own walls
    frame = box(inset, inset, width_mm - inset, depth_mm - inset)

    def ground(xy: np.ndarray) -> np.ndarray:
        return _sample(relief, width_mm, depth_mm, xy) + base_mm

    def points(geom) -> np.ndarray:
        return shapely.get_coordinates(shapely.segmentize(geom, DENSIFY_MM))

    shapes = [shapely.transform(p, to_model) for p in outlines]
    for line, cls in lines:
        w = max(BRIDGE_WIDTH_M.get(cls, DEFAULT_WIDTH_M) * mm_per_m, min_width_mm)
        shapes.append(shapely.transform(line, to_model).buffer(w / 2, quad_segs=4))
    if not shapes or not water:
        return BuildingMesh.empty()
    water_m = shapely.union_all([shapely.transform(p, to_model) for p in water])
    # Opening drops slivers, where outlines and lines nearly coincide.
    open_mm = min_width_mm / 8
    footprint = unary_union(shapes).buffer(-open_mm).buffer(open_mm, join_style="mitre")
    footprint = shapely.make_valid(footprint).intersection(frame)
    landing = max(landing_m * mm_per_m, min_landing_mm)
    big = 10.0 * (width_mm + depth_mm)

    verts, roofs, walls = [], [], []
    offset = 0
    count = 0

    def add(prism) -> bool:
        nonlocal offset
        if prism is None:
            return False
        v, r, w = prism
        verts.append(v)
        roofs.append(r + offset)
        walls.append(w + offset)
        offset += v.shape[0]
        return True

    for part in _polygons(footprint):
        if part.intersection(water_m).area < min_area_mm2:
            continue
        reach = water_m.intersection(part.buffer(landing)).buffer(landing)
        for piece in _polygons(part.intersection(reach)):
            wet = piece.intersection(water_m)
            if wet.area < min_area_mm2:
                continue
            # The long axis of the bridge, and the stretch of it over water.
            with np.errstate(all="ignore"):  # GEOS trips numpy's float flags
                rect = np.asarray(piece.minimum_rotated_rectangle.exterior.coords)
            e0, e1 = rect[1] - rect[0], rect[2] - rect[1]
            u = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1
            u = u / np.hypot(*u)
            angle = math.atan2(u[1], u[0])
            wet_t = shapely.get_coordinates(wet) @ u
            # Landings are the land at the ends; land between stretches of
            # water, such as a tower's island, stays under the span.
            lands = [
                p
                for p in _polygons(piece.difference(water_m))
                if p.area >= min_area_mm2 and _beyond(p, u, wet_t.min(), wet_t.max())
            ]
            span = piece.difference(unary_union(lands)) if lands else piece
            land_z = [ground(points(p)) for p in lands]
            tops = [float(z.max()) + min_height_mm for z in land_z]
            level = float(ground(points(wet)).max())  # the water under the span

            made = False
            for p, z, top in zip(lands, land_z, tops, strict=True):
                made |= add(_prism(p, float(z.min()) - sink_mm, top))

            # Cells along the long axis of the bridge: decks, with a pier
            # at each of the evenly spaced cuts between them.
            t = shapely.get_coordinates(span) @ u
            t0, t1 = float(t.min()), float(t.max())
            n = math.ceil((t1 - t0) / pier_spacing_mm) if pier_spacing_mm > 0 else 1
            cells, start = [], t0 - 1.0
            for k in range(1, n):
                c = t0 + (t1 - t0) * k / n
                cells += [(start, c - pier_mm / 2, False)]
                cells += [(c - pier_mm / 2, c + pier_mm / 2, True)]
                start = c + pier_mm / 2
            cells.append((start, t1 + 1.0, False))
            for a, b, is_pier in cells:
                strip = affinity.rotate(
                    box(a, -big, b, big), angle, origin=(0, 0), use_radians=True
                )
                for cell in _polygons(span.intersection(strip)):
                    if cell.area < min_area_mm2:
                        continue
                    for prism in _cell_prisms(cell):
                        v = prism[0]
                        k = v.shape[0] // 2
                        v[:k, 2] = _deck_top(
                            v[:k, :2], lands, tops, level, clearance_mm, ease_mm
                        )
                        if is_pier:
                            v[k:, 2] = float(ground(v[:k, :2]).min()) - sink_mm
                        else:
                            v[k:, 2] = v[:k, 2] - deck_mm
                        made |= add(prism)
            count += made

    if not verts:
        return BuildingMesh.empty()
    return BuildingMesh(
        vertices=np.vstack(verts).astype(np.float32),
        roof_faces=np.vstack(roofs),
        wall_faces=np.vstack(walls),
        count=count,
    )
