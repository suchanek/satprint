"""Water map for multi-color prints: which terrain grid cells are water.

Water shapes come from the ``water`` layer of the same OpenFreeMap vector
tiles the buildings use (sea, rivers, lakes, ponds). Where the sea was
flattened to 0 m, the flattened cells count as water too, so a coast still
colors when the tiles are unavailable.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely import Polygon

from .buildings import model_projection, vector_tile_features
from .mesh import BuildingMesh, Mesh, heightmap_split_solids
from .terrain import BBox, _tile_count

# OpenMapTiles water classes worth coloring; swimming pools (often on
# roofs) are left out.
WATER_CLASSES = {"ocean", "sea", "lake", "river", "pond", "reservoir", "basin", "dock"}
MAX_WATER_TILES = 16  # water zoom drops until the area fits in this many tiles
# Display colors for the 3MF parts; the slicer picks the filaments.
PART_COLORS = {
    "land": "#8A9A5B",
    "water": "#2F6FB3",
    "buildings": "#E8E8E8",
    "border": "#3A3A3A",
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
) -> tuple[list[tuple[str, Mesh, str]], np.ndarray]:
    """The 3MF parts, land, water, buildings and border, and the water mask.

    Parts without geometry are left out, so the filament numbers stay in this
    order: land 1, water 2, then buildings and border as present.

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
    if frame is not None:
        parts.append(("border", frame, PART_COLORS["border"]))
    return parts, mask
