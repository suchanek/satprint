"""Water map for multi-color prints: which terrain grid cells are water.

Water shapes come from the ``water`` layer of the same OpenFreeMap vector
tiles the buildings use (sea, rivers, lakes, ponds). Where the sea was
flattened to 0 m, the flattened cells count as water too, so a coast still
colors when the tiles are unavailable.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import shapely
from shapely import Polygon
from shapely.ops import unary_union

from .buildings import _polygons, model_projection, vector_tile_features
from .mesh import BuildingMesh, Mesh, heightmap_split_solids, merge_building_meshes
from .terrain import BBox, Heightmap, _tile_count

# OpenMapTiles water classes worth coloring; swimming pools (often on
# roofs) are left out.
WATER_CLASSES = {"ocean", "sea", "lake", "river", "pond", "reservoir", "basin", "dock"}
# Water that lies flat. A river runs downhill, so it is left as the data has it.
STILL_WATER = {"ocean", "sea", "lake", "reservoir", "pond", "basin"}
SHORE_PX = (1.0, 3.0)  # the shore sampled between these distances from the water
MAX_WATER_TILES = 16  # water zoom drops until the area fits in this many tiles
# Display colors for the 3MF parts. Parts of one color share a filament, so
# roads and border print in one gray and the model needs at most four
# filaments, what an AMS lite holds.
PART_COLORS = {
    "land": "#8A9A5B",
    "water": "#2F6FB3",
    "buildings": "#E8E8E8",
    "roads": "#4A4A4A",
    "border": "#4A4A4A",
}


def water_zoom(bbox: BBox) -> int:
    """Highest vector-tile zoom (8-14) covering ``bbox`` in MAX_WATER_TILES.

    A city reuses the zoom-14 tiles its buildings came from.
    """
    zoom = 14
    while zoom > 8 and _tile_count(bbox, zoom) > MAX_WATER_TILES:
        zoom -= 1
    return zoom


def water_from_vector_tiles(tiles: list[tuple[int, int, int, bytes]]) -> list[Polygon]:
    """Water polygons, in lon/lat, from the tiles' ``water`` layer."""
    return [
        poly
        for poly, props in vector_tile_features(tiles, "water")
        if props.get("class", "lake") in WATER_CLASSES
    ]


def level_water(hm: Heightmap, tiles: list[tuple[int, int, int, bytes]]) -> Heightmap:
    """``hm`` with each lake, pond, reservoir and sea surface flattened to its shore.

    Elevation data over water is unreliable: radar returns and void filling
    leave noise, and a lake in a steep valley can read tens of meters above
    its shore, which prints as a raised plateau. Each water body is set to
    the 10th percentile of the ground along its shore, the lower shore, and
    only ever lowered, so a body the data already shows flat is unchanged.

    :param hm: a heightmap with a bounding box.
    :param tiles: vector tiles covering it, as :meth:`VectorTileClient.tiles` returns.
    :return: a new heightmap, or ``hm`` itself when there is no still water.
    """
    if hm.bbox is None:
        return hm
    rows, cols = hm.data.shape
    shapes = [
        poly
        for poly, props in vector_tile_features(tiles, "water")
        if props.get("class", "lake") in STILL_WATER
    ]
    if not shapes:
        return hm
    # one pixel per unit, x east and y north, so a node (r, c) is at (c, rows-1-r);
    # pieces cut at a tile edge are joined first so each body has one shore
    to_px = model_projection(hm.bbox, cols - 1, rows - 1)
    bodies = _polygons(unary_union([shapely.transform(p, to_px) for p in shapes]))
    # The tiles outline a coast far finer than the grid; buffering that is
    # slow and uses gigabytes, so keep the detail to a fraction of a node.
    bodies = [b for body in bodies for b in _polygons(body.simplify(0.25))]
    data = hm.data.copy()
    changed = False
    for body in bodies:
        x0, y0, x1, y1 = body.buffer(SHORE_PX[1], quad_segs=2).bounds
        c0, c1 = max(int(np.floor(x0)), 0), min(int(np.ceil(x1)), cols - 1)
        r0, r1 = (
            max(int(np.floor(rows - 1 - y1)), 0),
            min(int(np.ceil(rows - 1 - y0)), rows - 1),
        )
        if c1 < c0 or r1 < r0:
            continue
        gx, gy = np.meshgrid(np.arange(c0, c1 + 1), rows - 1 - np.arange(r0, r1 + 1))
        inside = shapely.contains_xy(body, gx, gy)
        shore = shapely.contains_xy(
            body.buffer(SHORE_PX[1], quad_segs=2), gx, gy
        ) & ~shapely.contains_xy(body.buffer(SHORE_PX[0], quad_segs=2), gx, gy)
        if inside.sum() < 4 or not shore.any():
            continue
        window = data[r0 : r1 + 1, c0 : c1 + 1]
        level = np.percentile(window[shore], 10)
        lowered = inside & (window > level)
        window[lowered] = level
        changed |= bool(lowered.any())
    return replace(hm, data=data) if changed else hm


def _fix_diagonals(mask: np.ndarray, max_rounds: int = 50) -> np.ndarray:
    """Remove 2x2 checkerboards, where two cells of a class touch only at a
    corner. The split solids would share just that vertical edge, which is
    non-manifold. The gap is filled with water each time."""
    m = mask.copy()
    for _ in range(max_rounds):
        a, b = m[:-1, :-1], m[:-1, 1:]
        c, d = m[1:, :-1], m[1:, 1:]
        diag = (a == d) & (b == c) & (a != b)
        if not diag.any():
            break
        r, k = np.nonzero(diag)
        # Water on the main diagonal: fill the top-right cell. Land on it:
        # fill the top-left one. Either way three of the four are water.
        wet = m[r, k]
        m[r[wet], k[wet] + 1] = True
        m[r[~wet], k[~wet]] = True
    return m


def water_mask(
    polygons: list[Polygon],
    bbox: BBox,
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    sea_level_flat: bool = False,
) -> np.ndarray:
    """(rows-1, cols-1) boolean grid: True where a terrain cell is water.

    :param polygons: water shapes in lon/lat.
    :param bbox: area the block covers.
    :param relief_mm: the relief passed to :func:`heightmap_to_mesh`.
    :param width_mm: block width.
    :param depth_mm: block depth.
    :param sea_level_flat: the sea was flattened to the lowest relief, so cells
        lying flat there count as water.
    :return: the mask, row 0 north, as the heightmap.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    rows, cols = relief.shape
    xs = (np.arange(cols - 1) + 0.5) / (cols - 1) * width_mm
    ys = depth_mm - (np.arange(rows - 1) + 0.5) / (rows - 1) * depth_mm
    gx, gy = np.meshgrid(xs, ys)
    mask = np.zeros((rows - 1, cols - 1), dtype=bool)
    if polygons:
        to_model = model_projection(bbox, width_mm, depth_mm)
        water = shapely.union_all([shapely.transform(p, to_model) for p in polygons])
        shapely.prepare(water)
        mask = shapely.contains_xy(water, gx, gy)
    if sea_level_flat:
        lo = np.min(relief)
        corners = np.max(
            np.stack(
                [relief[:-1, :-1], relief[:-1, 1:], relief[1:, :-1], relief[1:, 1:]]
            ),
            axis=0,
        )
        mask |= corners <= lo + 1e-6
    return _fix_diagonals(mask)


def multicolor_parts(
    polygons: list[Polygon],
    bbox: BBox,
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
    sea_level_flat: bool = False,
    buildings: BuildingMesh | None = None,
    frame: Mesh | None = None,
    roads: BuildingMesh | None = None,
    bridges: BuildingMesh | None = None,
) -> tuple[list[tuple[str, Mesh, str]], np.ndarray]:
    """The 3MF parts, land, water, buildings, roads and border, and the water
    mask.

    Bridges go in the roads part, so they print in the road color. Parts
    without geometry are left out; :func:`write_3mf` numbers the filaments by
    color in this order: land 1, water 2, then buildings and the gray of roads
    and border as present.

    :return: ([(name, mesh, color), ...] for :func:`write_3mf`, mask).
    """
    mask = water_mask(polygons, bbox, relief_mm, width_mm, depth_mm, sea_level_flat)
    water, land = heightmap_split_solids(relief_mm, width_mm, depth_mm, base_mm, mask)
    parts = [
        ("land", land, PART_COLORS["land"]),
        ("water", water, PART_COLORS["water"]),
    ]
    if buildings is not None and buildings.count:
        parts.append(("buildings", buildings.as_mesh(), PART_COLORS["buildings"]))
    deck = merge_building_meshes(*(m for m in (roads, bridges) if m is not None))
    if deck.count:
        parts.append(("roads", deck.as_mesh(), PART_COLORS["roads"]))
    if frame is not None:
        parts.append(("border", frame, PART_COLORS["border"]))
    return parts, mask
