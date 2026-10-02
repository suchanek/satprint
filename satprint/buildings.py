"""OpenStreetMap buildings -> closed solids standing on the terrain.

Each building is a prism: a flat roof at its height above the highest
terrain under it, walls, and a floor sunk slightly below the lowest terrain
under it so it fuses with the base when sliced. Every prism is closed and
outward-wound on its own, so the merged STL stays edge-manifold.

Two sources, both OpenStreetMap data:

* OpenFreeMap vector tiles (:func:`buildings_from_vector_tiles`): heights are
  the tiles' ``render_height``, already worked out from the OSM tags, and an
  outline whose parts are mapped is flagged ``hide_3d``.
* Overpass (:func:`buildings_from_osm`): raw OSM. Heights come from the
  ``height`` tag, then ``building:levels``, then a default.

Either way, where a building has ``building:part`` shapes the parts are used
and the outline is dropped, as the OSM Simple 3D Buildings scheme specifies.
A part's minimum height is ignored: parts are extruded from the ground so
nothing floats in a print.
"""

from __future__ import annotations

import gzip
import math
import re
from dataclasses import dataclass

import mapbox_vector_tile
import numpy as np
import shapely
from shapely import LineString, MultiPolygon, Polygon, STRtree, box
from shapely.geometry import shape
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from .mesh import BuildingMesh
from .terrain import BBox, Progress

LEVEL_HEIGHT_M = 3.0
DEFAULT_HEIGHT_M = 8.0
MAX_BUILDING_AREA_KM2 = 40.0  # Overpass responses grow fast with area

_FEET = re.compile(r"^\s*([\d.]+)\s*(ft|')\s*$")
_NUMBER = re.compile(r"^\s*([\d.]+)\s*(m)?\s*$")


@dataclass(frozen=True)
class Building:
    footprint: Polygon  # lon/lat
    height_m: float
    is_part: bool = False


def parse_height(tags: dict) -> float:
    """Building height in meters from OSM tags."""
    raw = tags.get("height", "").split(";")[0]
    if m := _FEET.match(raw):
        return float(m.group(1)) * 0.3048
    if m := _NUMBER.match(raw):
        try:
            return float(m.group(1))
        except ValueError:
            pass
    levels = tags.get("building:levels", "").split(";")[0].strip()
    try:
        return max(1.0, float(levels)) * LEVEL_HEIGHT_M
    except ValueError:
        return DEFAULT_HEIGHT_M


def _ring(geometry: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in geometry]


def _relation_polygon(rel: dict) -> Polygon | MultiPolygon | None:
    """Assemble a multipolygon relation from its member ways."""
    outer, inner = [], []
    for m in rel.get("members", []):
        if m.get("type") != "way" or len(m.get("geometry", [])) < 2:
            continue
        line = LineString(_ring(m["geometry"]))
        (inner if m.get("role") == "inner" else outer).append(line)
    if not outer:
        return None
    shell = unary_union(list(polygonize(outer)))
    if inner:
        shell = shell.difference(unary_union(list(polygonize(inner))))
    return shell if not shell.is_empty else None


def _polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in _polygons(g)]
    return []


def buildings_from_osm(data: dict) -> list[Building]:
    """Parse an Overpass ``out body geom`` response into buildings."""
    outlines: list[Building] = []
    parts: list[Building] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        is_part = "building:part" in tags and tags["building:part"] != "no"
        is_building = tags.get("building", "no") != "no"
        if not (is_part or is_building):
            continue
        if tags.get("location") == "underground" or tags.get("layer", "0").startswith(
            "-"
        ):
            continue
        if el["type"] == "way":
            pts = _ring(el.get("geometry", []))
            if len(pts) < 4 or pts[0] != pts[-1]:
                continue
            geom = Polygon(pts)
        elif el["type"] == "relation":
            geom = _relation_polygon(el)
        else:
            continue
        height = parse_height(tags)
        for poly in _polygons(shapely.make_valid(geom) if geom is not None else None):
            if poly.area > 0:
                (parts if is_part else outlines).append(Building(poly, height, is_part))

    if parts:
        tree = STRtree([p.footprint for p in parts])
        points = [p.footprint.representative_point() for p in parts]
        kept = []
        for b in outlines:
            hits = tree.query(b.footprint)
            if not any(b.footprint.contains(points[i]) for i in hits):
                kept.append(b)
        outlines = kept
    return outlines + parts


def vector_tile_features(
    tiles: list[tuple[int, int, int, bytes]], layer: str
) -> list[tuple[Polygon, dict]]:
    """Polygons of one layer of OpenMapTiles vector tiles, in lon/lat.

    Each feature is clipped to its own tile, without the tile buffer, so a
    shape crossing a tile edge comes back as two pieces that meet at the
    edge instead of two overlapping copies.
    """
    out: list[tuple[Polygon, dict]] = []
    for z, x, y, data in tiles:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        found = mapbox_vector_tile.decode(
            data, default_options={"y_coord_down": True}
        ).get(layer)
        if not found:
            continue
        extent = found["extent"]
        frame = box(0, 0, extent, extent)
        n = 2**z

        def to_lonlat(c: np.ndarray, x=x, y=y, extent=extent, n=n) -> np.ndarray:
            gx = (x + c[:, 0] / extent) / n
            gy = (y + c[:, 1] / extent) / n
            lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * gy))))
            return np.column_stack([gx * 360.0 - 180.0, lat])

        for f in found["features"]:
            if f["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
                continue
            geom = shapely.make_valid(shape(f["geometry"])).intersection(frame)
            for poly in _polygons(geom):
                if poly.area > 0:
                    out.append((shapely.transform(poly, to_lonlat), f["properties"]))
    return out


def buildings_from_vector_tiles(
    tiles: list[tuple[int, int, int, bytes]],
) -> list[Building]:
    """Parse the ``building`` layer of OpenMapTiles vector tiles."""
    out: list[Building] = []
    for poly, props in vector_tile_features(tiles, "building"):
        if props.get("hide_3d"):
            continue  # an outline drawn by its building:part features
        height = float(props.get("render_height") or DEFAULT_HEIGHT_M)
        is_part = float(props.get("render_min_height") or 0) > 0
        out.append(Building(poly, height, is_part))
    return out


def _mercator(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Normalized Web Mercator, x right and y down, both 0..1 over the world."""
    lat_r = np.radians(lat)
    mx = (lon + 180.0) / 360.0
    my = (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / math.pi) / 2.0
    return mx, my


def model_projection(bbox: BBox, width_mm: float, depth_mm: float):
    """lon/lat (n, 2) -> model mm (n, 2) for a block covering ``bbox``.

    Linear in Web Mercator, as the heightmap and texture are, with X east
    and Y north.
    """
    mx0, my0 = _mercator(np.array([bbox.west]), np.array([bbox.north]))
    mx1, my1 = _mercator(np.array([bbox.east]), np.array([bbox.south]))

    def to_model(coords: np.ndarray) -> np.ndarray:
        mx, my = _mercator(coords[:, 0], coords[:, 1])
        x = (mx - mx0) / (mx1 - mx0) * width_mm
        y = (my1 - my) / (my1 - my0) * depth_mm
        return np.column_stack([x, y])

    return to_model


def _sample(
    relief: np.ndarray, width_mm: float, depth_mm: float, xy: np.ndarray
) -> np.ndarray:
    """Bilinear relief height (mm above the base) at model coordinates."""
    rows, cols = relief.shape
    fc = np.clip(xy[:, 0] / width_mm * (cols - 1), 0, cols - 1)
    fr = np.clip((depth_mm - xy[:, 1]) / depth_mm * (rows - 1), 0, rows - 1)
    c0 = np.minimum(fc.astype(int), cols - 2)
    r0 = np.minimum(fr.astype(int), rows - 2)
    tc, tr = fc - c0, fr - r0
    top = relief[r0, c0] * (1 - tc) + relief[r0, c0 + 1] * tc
    bot = relief[r0 + 1, c0] * (1 - tc) + relief[r0 + 1, c0 + 1] * tc
    return top * (1 - tr) + bot * tr


def _prism(
    poly: Polygon, z_bottom: float, z_top: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Closed prism over ``poly``: (vertices, roof faces, wall+floor faces)."""
    poly = orient(poly, sign=1.0)  # exterior CCW, holes CW
    rings = [np.asarray(poly.exterior.coords)[:-1]] + [
        np.asarray(r.coords)[:-1] for r in poly.interiors
    ]
    ring_xy = np.vstack(rings)
    n = ring_xy.shape[0]
    index = {(float(x), float(y)): i for i, (x, y) in enumerate(ring_xy)}
    if len(index) != n:
        return None  # repeated vertex; the walls would not close

    roof = []
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        pts = np.asarray(tri.exterior.coords)[:3]
        try:
            ids = [index[(float(x), float(y))] for x, y in pts]
        except KeyError:
            return None
        a, b, c = pts
        if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0:
            ids = [ids[0], ids[2], ids[1]]
        roof.append(ids)
    if not roof:
        return None
    roof_f = np.asarray(roof, dtype=np.int64)

    walls = []
    start = 0
    for ring in rings:
        k = ring.shape[0]
        t0 = start + np.arange(k)
        t1 = start + (np.arange(k) + 1) % k
        b0, b1 = t0 + n, t1 + n
        # Outward for CCW exteriors and CW holes alike.
        walls.append(np.column_stack([b0, b1, t1]))
        walls.append(np.column_stack([b0, t1, t0]))
        start += k
    floor = roof_f[:, ::-1] + n
    vertices = np.vstack(
        [
            np.column_stack([ring_xy, np.full(n, z_top)]),
            np.column_stack([ring_xy, np.full(n, z_bottom)]),
        ]
    )
    return vertices, roof_f, np.vstack(walls + [floor])


def building_mesh(
    buildings: list[Building],
    bbox: BBox,
    relief_mm: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
    mm_per_m: float,
    scale: float = 1.0,
    sink_mm: float = 0.3,
    min_height_mm: float = 0.2,
    min_area_mm2: float = 0.05,
    simplify_mm: float = 0.05,
    progress: Progress | None = None,
) -> BuildingMesh:
    """Place ``buildings`` on the terrain block built from ``relief_mm``.

    :param buildings: footprints in lon/lat with heights.
    :param bbox: area the terrain block covers.
    :param relief_mm: the relief passed to :func:`heightmap_to_mesh`.
    :param width_mm: block width.
    :param depth_mm: block depth.
    :param base_mm: base thickness under the lowest terrain.
    :param mm_per_m: horizontal model scale; building heights use it too,
        so ``scale=1`` keeps buildings in true proportion.
    :param scale: building height multiplier.
    :param sink_mm: how far floors go below the terrain.
    :param min_height_mm: lowest building height, so small ones still print.
    :param min_area_mm2: footprints smaller than this are dropped.
    :param simplify_mm: footprint simplification tolerance.
    :param progress: called as ``progress("building mesh", done, total)``.
    :return: all buildings as one :class:`BuildingMesh`.
    """
    relief = np.asarray(relief_mm, dtype=np.float64)
    if np.min(relief) < 0:  # match heightmap_to_mesh
        relief = relief - np.min(relief)
    to_model = model_projection(bbox, width_mm, depth_mm)
    inset = 0.05  # keep walls off the block's own walls
    frame = box(inset, inset, width_mm - inset, depth_mm - inset)
    verts, roofs, walls = [], [], []
    offset = 0
    count = 0
    footprints: list[tuple[Polygon, float]] = []
    for n_done, b in enumerate(buildings):
        if progress and n_done % 500 == 0:
            progress("building mesh", n_done, len(buildings))
        model = shapely.transform(b.footprint, to_model)
        clipped = shapely.make_valid(model.intersection(frame))
        h_mm = max(b.height_m * mm_per_m * scale, min_height_mm)
        for poly in _polygons(clipped):
            poly = poly.simplify(simplify_mm, preserve_topology=True)
            if isinstance(poly, Polygon) and poly.area >= min_area_mm2:
                footprints.append((poly, h_mm))
    for poly, h_mm in resolve_overlaps(footprints):
        if poly.area < min_area_mm2:
            continue
        ring_pts = np.vstack(
            [np.asarray(poly.exterior.coords)]
            + [np.asarray(r.coords) for r in poly.interiors]
        )
        ground = _sample(relief, width_mm, depth_mm, ring_pts) + base_mm
        prism = _prism(poly, float(ground.min()) - sink_mm, float(ground.max()) + h_mm)
        if prism is None:
            continue
        v, r, w = prism
        verts.append(v)
        roofs.append(r + offset)
        walls.append(w + offset)
        offset += v.shape[0]
        count += 1
    if not verts:
        return BuildingMesh.empty()
    return BuildingMesh(
        vertices=np.vstack(verts).astype(np.float32),
        roof_faces=np.vstack(roofs),
        wall_faces=np.vstack(walls),
        count=count,
    )


def resolve_overlaps(
    footprints: list[tuple[Polygon, float]], tolerance: float = 1e-6
) -> list[tuple[Polygon, float]]:
    """Replace overlapping footprints with non-overlapping pieces.

    OSM footprints overlap: a tower's parts stack over its base, and
    neighboring outlines cross by a few centimeters. Extruded as they are,
    those prisms pass through each other, which slicers report as invalid
    geometry. Within each group of overlapping footprints, every piece of
    the overlay takes the tallest height covering it, and pieces of equal
    height are merged, so the solids only touch along shared walls.
    Footprints that overlap nothing pass through unchanged.

    :param footprints: (polygon, height) pairs, in any planar units.
    :param tolerance: overlap area below which two footprints count as only
        touching.
    :return: (polygon, height) pairs with no overlapping interiors.
    """
    if len(footprints) < 2:
        return list(footprints)
    polys = np.array([p for p, _ in footprints], dtype=object)
    heights = np.array([h for _, h in footprints])
    a, b = STRtree(polys).query(polys, predicate="intersects")
    keep = a < b
    a, b = a[keep], b[keep]
    if a.size:
        real = shapely.area(shapely.intersection(polys[a], polys[b])) > tolerance
        a, b = a[real], b[real]

    parent = list(range(len(polys)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in zip(a.tolist(), b.tolist(), strict=True):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(len(polys)):
        groups.setdefault(find(i), []).append(i)

    out: list[tuple[Polygon, float]] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(footprints[members[0]])
            continue
        lines = shapely.union_all([polys[i].boundary for i in members])
        by_height: dict[float, list[Polygon]] = {}
        for face in polygonize(lines.geoms if hasattr(lines, "geoms") else [lines]):
            pt = face.representative_point()
            covering = [heights[i] for i in members if polys[i].covers(pt)]
            if covering:  # else a hole enclosed by the group
                by_height.setdefault(round(float(max(covering)), 4), []).append(face)
        for h, faces in by_height.items():
            for poly in _polygons(shapely.make_valid(unary_union(faces))):
                if poly.area > tolerance:
                    out.append((poly, h))
    return out


def bbox_area_km2(bbox: BBox) -> float:
    w, h = bbox.ground_size_m()
    return w * h / 1e6
