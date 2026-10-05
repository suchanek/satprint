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

Roofs tagged ``roof:shape`` dome, onion, cone or pyramidal get that shape
(:data:`ROOF_PROFILES`); every other roof is flat. The vector tiles carry no
roof tags, so :func:`apply_shapes` merges in the shaped buildings from a small
Overpass query. A few landmarks are replaced by exact shapes from
:mod:`satprint.landmarks`. Those a prism cannot express, such as an arch or
separate legs, carry a closed triangle mesh in :attr:`Building.solid`, which
:func:`building_mesh` places on the terrain as it is.
"""

from __future__ import annotations

import gzip
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, overload

import mapbox_vector_tile
import numpy as np
import shapely
from shapely import LineString, MultiPolygon, Polygon, STRtree, affinity, box
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


Profile = tuple[tuple[float, float], ...]


def _dome(n: int = 10) -> Profile:
    return tuple(
        (math.cos(t), math.sin(t))
        for t in (math.pi / 2 * i / n for i in range(1, n + 1))
    )


# Rings above the eave as (scale toward the footprint's centroid, fraction of
# the roof height); a final scale of 0 is the apex.
ROOF_PROFILES: dict[str, Profile] = {
    "dome": _dome(),
    "onion": (
        (1.15, 0.12),
        (1.22, 0.28),
        (1.12, 0.45),
        (0.80, 0.62),
        (0.45, 0.76),
        (0.20, 0.88),
        (0.08, 0.95),
        (0.0, 1.0),
    ),
    "cone": ((0.0, 1.0),),
    "pyramidal": ((0.0, 1.0),),
}


@dataclass(frozen=True)
class Building:
    footprint: Polygon  # lon/lat
    height_m: float
    is_part: bool = False
    profile: Profile = ()  # roof rings; empty for a flat roof
    roof_height_m: float = 0.0  # included in height_m
    osm_id: str = ""  # "way/123", where the source says
    wikidata: str = ""
    # Called with the minimum feature size in meters; returns (vertices, faces):
    # lon, lat and meters above the ground, and a closed, outward-wound mesh.
    # ``footprint`` is then its plan outline and ``height_m`` its height.
    solid: Callable[[float], tuple[np.ndarray, np.ndarray]] | None = None


def _parse_length(raw: str) -> float | None:
    """Meters from an OSM length tag ("45", "45 m", "100 ft"), else None."""
    raw = raw.split(";")[0]
    if m := _FEET.match(raw):
        return float(m.group(1)) * 0.3048
    if m := _NUMBER.match(raw):
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def parse_height(tags: dict) -> float:
    """Building height in meters from OSM tags."""
    if (h := _parse_length(tags.get("height", ""))) is not None:
        return h
    levels = tags.get("building:levels", "").split(";")[0].strip()
    try:
        return max(1.0, float(levels)) * LEVEL_HEIGHT_M
    except ValueError:
        return DEFAULT_HEIGHT_M


def radius_m(footprint: Polygon) -> float:
    """Radius of the circle with the footprint's area, for a lon/lat polygon."""
    lat = footprint.centroid.y
    area = footprint.area * 111_320.0**2 * math.cos(math.radians(lat))
    return math.sqrt(area / math.pi)


def roof_shape(
    shape: str | None, roof_height_m: float | None, height_m: float, footprint: Polygon
) -> tuple[Profile, float]:
    """(profile, roof height) for a ``roof:shape`` value; flat if unsupported.

    :param shape: the ``roof:shape`` value.
    :param roof_height_m: the tagged roof height, or None to use the
        footprint's radius, a hemisphere for a dome.
    :param height_m: total height; the roof never exceeds it.
    :param footprint: lon/lat footprint.
    """
    profile = ROOF_PROFILES.get((shape or "").strip().lower(), ())
    if not profile:
        return (), 0.0
    if roof_height_m is None:
        roof_height_m = radius_m(footprint)
    return profile, max(0.0, min(roof_height_m, height_m))


def parse_roof(
    tags: dict, height_m: float, footprint: Polygon
) -> tuple[Profile, float]:
    """(profile, roof height) from OSM ``roof:shape``, ``roof:height`` and
    ``roof:levels`` tags."""
    rh = _parse_length(tags.get("roof:height", ""))
    if rh is None:
        try:
            rh = float(tags.get("roof:levels", "").split(";")[0]) * LEVEL_HEIGHT_M
        except ValueError:
            rh = None
    return roof_shape(tags.get("roof:shape"), rh, height_m, footprint)


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


def _lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in _lines(g)]
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
                profile, roof_h = parse_roof(tags, height, poly)
                (parts if is_part else outlines).append(
                    Building(
                        poly,
                        height,
                        is_part,
                        profile,
                        roof_h,
                        osm_id=f"{el['type']}/{el.get('id')}",
                        wikidata=tags.get("wikidata", ""),
                    )
                )

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


@overload
def vector_tile_features(
    tiles: list[tuple[int, int, int, bytes]], layer: str, lines: Literal[False] = ...
) -> list[tuple[Polygon, dict]]: ...


@overload
def vector_tile_features(
    tiles: list[tuple[int, int, int, bytes]], layer: str, lines: Literal[True]
) -> list[tuple[LineString, dict]]: ...


def vector_tile_features(
    tiles: list[tuple[int, int, int, bytes]], layer: str, lines: bool = False
) -> list[tuple[Any, dict]]:
    """Polygons of one layer of OpenMapTiles vector tiles, in lon/lat, or
    its lines with ``lines=True``.

    Each feature is clipped to its own tile, without the tile buffer, so a
    shape crossing a tile edge comes back as two pieces that meet at the
    edge instead of two overlapping copies.
    """
    kinds = ("LineString", "MultiLineString") if lines else ("Polygon", "MultiPolygon")
    out: list[tuple[Any, dict]] = []
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
            if f["geometry"]["type"] not in kinds:
                continue
            geom = shape(f["geometry"])
            if lines:
                parts = _lines(geom.intersection(frame))
            else:
                parts = [
                    p
                    for p in _polygons(shapely.make_valid(geom).intersection(frame))
                    if p.area > 0
                ]
            for part in parts:
                out.append((shapely.transform(part, to_lonlat), f["properties"]))
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


def apply_shapes(buildings: list[Building], shaped: list[Building]) -> list[Building]:
    """Swap in ``shaped`` buildings for the plain copies of them in ``buildings``.

    A plain building is a copy of a shaped one when it contains the shaped
    footprint's representative point, is within a factor of two of its area
    and has its height, within the tiles' rounding to whole meters. Other
    overlaps, such as the drum under a dome, are left to
    :func:`building_mesh`.

    :param buildings: buildings from a source without roof tags.
    :param shaped: buildings with roof profiles, from Overpass.
    :return: ``buildings`` with the copies replaced.
    """
    shaped = [s for s in shaped if s.profile]
    if not shaped or not buildings:
        return list(buildings) + shaped
    tree = STRtree([b.footprint for b in buildings])
    drop: set[int] = set()
    for s in shaped:
        pt = s.footprint.representative_point()
        for i in tree.query(pt, predicate="intersects"):
            b = buildings[i]
            ratio = b.footprint.area / s.footprint.area
            if 0.5 <= ratio <= 2.0 and abs(b.height_m - s.height_m) <= 1.5:
                drop.add(int(i))
    return [b for i, b in enumerate(buildings) if i not in drop] + shaped


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
    poly: Polygon,
    z_bottom: float,
    z_top: float,
    profile: Profile = (),
    roof_mm: float = 0.0,
    hole_top: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Closed prism over ``poly``: (vertices, roof faces, wall+floor faces).

    With a ``profile`` the flat roof is replaced by rings of the exterior
    scaled toward the centroid, rising ``roof_mm`` above ``z_top``. The
    caller makes sure ``poly`` is star-shaped about its centroid. A shaped
    roof may have one hole, for what stands on it: then the profile stops
    short of the apex, its last ring is joined to the hole's edge at
    ``hole_top``, and the hole's walls run from there down to the floor.
    """
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
    k = rings[0].shape[0]
    top_z = np.full(n, z_top)
    if hole_top is not None:
        top_z[k:] = hole_top
    vertices = [
        np.column_stack([ring_xy, top_z]),
        np.column_stack([ring_xy, np.full(n, z_bottom)]),
    ]
    if profile:
        roof_f = _profile_faces(poly, ring_xy, k, z_top, profile, roof_mm, vertices)
        if roof_f is None:
            return None
    return np.vstack(vertices), roof_f, np.vstack(walls + [floor])


def _profile_faces(
    poly: Polygon,
    ring_xy: np.ndarray,
    k: int,
    z_top: float,
    profile: Profile,
    roof_mm: float,
    vertices: list[np.ndarray],
) -> np.ndarray | None:
    """Faces of a shaped roof over the exterior ring (top indices ``0..k-1``),
    appending its new vertices to ``vertices``. Without an apex, the last
    ring is joined to the hole (top indices ``k..``)."""
    ext = ring_xy[:k]
    c = np.asarray(Polygon(ext).centroid.coords[0])
    nxt = sum(v.shape[0] for v in vertices)
    prev, prev_xy = np.arange(k), ext
    roll = (np.arange(k) + 1) % k
    faces = []
    for scale, frac in profile:
        z = z_top + frac * roof_mm
        if scale <= 1e-9:
            vertices.append(np.array([[c[0], c[1], z]]))
            faces.append(np.column_stack([prev, prev[roll], np.full(k, nxt)]))
            return np.vstack(faces)
        ring = c + (ext - c) * scale
        vertices.append(np.column_stack([ring, np.full(k, z)]))
        cur = nxt + np.arange(k)
        faces.append(np.column_stack([prev, prev[roll], cur[roll]]))
        faces.append(np.column_stack([prev, cur[roll], cur]))
        prev, prev_xy = cur, ring
        nxt += k
    if not poly.interiors:
        raise ValueError("a roof profile must end at an apex (scale 0)")
    # The flat ring between the last ring and the hole.
    hole = ring_xy[k:]
    index = {
        (float(x), float(y)): int(i) for (x, y), i in zip(prev_xy, prev, strict=True)
    }
    index.update({(float(x), float(y)): k + i for i, (x, y) in enumerate(hole)})
    for tri in shapely.constrained_delaunay_triangles(Polygon(prev_xy, [hole])).geoms:
        pts = np.asarray(tri.exterior.coords)[:3]
        try:
            ids = [index[(float(x), float(y))] for x, y in pts]
        except KeyError:
            return None
        a, b, d = pts
        if (b[0] - a[0]) * (d[1] - a[1]) - (b[1] - a[1]) * (d[0] - a[0]) < 0:
            ids = [ids[0], ids[2], ids[1]]
        faces.append(np.asarray([ids], dtype=np.int64))
    return np.vstack(faces)


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
    min_roof_mm: float = 0.3,
    min_feature_mm: float = 0.6,
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
    :param min_roof_mm: shaped roofs lower than this are printed flat, at the
        building's full height.
    :param min_feature_mm: thinnest part of a building that carries a
        :attr:`Building.solid` mesh, which is asked to honor it.
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
    shaped: list[tuple[Polygon, float, Profile, float]] = []
    meshes = [b.solid for b in buildings if b.solid is not None]
    for n_done, b in enumerate(buildings):
        if progress and n_done % 500 == 0:
            progress("building mesh", n_done, len(buildings))
        if b.solid is not None:
            continue  # placed below, outside the footprint pipeline
        model = shapely.transform(b.footprint, to_model)
        clipped = shapely.make_valid(model.intersection(frame))
        h_mm = max(b.height_m * mm_per_m * scale, min_height_mm)
        pieces = [
            p.simplify(simplify_mm, preserve_topology=True) for p in _polygons(clipped)
        ]
        pieces = [
            p for p in pieces if isinstance(p, Polygon) and p.area >= min_area_mm2
        ]
        roof_mm = min(b.roof_height_m * mm_per_m * scale, h_mm)
        if (
            b.profile
            and roof_mm >= min_roof_mm
            and len(pieces) == 1
            and clipped.area > 0.99 * model.area  # not cut by the block edge
            and _star_shaped(pieces[0])
        ):
            shaped.append((pieces[0], h_mm - roof_mm, b.profile, roof_mm))
        else:
            footprints.extend((p, h_mm) for p in pieces)

    # A shaped roof starts no lower than the flat roofs it stands among, so
    # a dome set into a taller wing is not left in a pit; if too little of
    # it shows, it is printed flat. Only footprints at least half its size
    # count. Smaller ones standing on it, such as a lantern on a dome, get a
    # hole cut for them (:func:`_lantern_hole`). The largest shaped solid
    # keeps its roof, one that reaches into it outside such a hole goes
    # flat, and flat footprints give up the area under every shaped one.
    flat_tree = STRtree([p for p, _ in footprints])
    raised = []
    for poly, eave, profile, roof_mm in shaped:
        top = eave + roof_mm
        around = [
            footprints[i][1]
            for i in flat_tree.query(poly, predicate="intersects")
            if footprints[i][0].area >= 0.5 * poly.area
            and footprints[i][0].intersection(poly).area > 0.05 * poly.area
        ]
        eave = max([eave, *around])
        if top - eave >= min_roof_mm:
            raised.append((poly, eave, profile, top - eave))
        else:
            footprints.append((poly, top))
    # A roof standing on a larger one goes after it; otherwise tallest first.
    inner = [r[0].buffer(-simplify_mm) for r in raised]
    depth = [
        sum(
            r[0].area < 0.5 * o[0].area and inner[j].covers(r[0])
            for j, o in enumerate(raised)
        )
        for r in raised
    ]
    order = sorted(
        range(len(raised)), key=lambda i: (depth[i], -(raised[i][1] + raised[i][3]))
    )
    raised = [raised[i] for i in order]
    # (polygon, height or eave, profile, roof height, hole top or None)
    solids: list[tuple[Polygon, float, Profile, float, float | None]] = []
    taken = Polygon()
    for n, (poly, eave, profile, roof_mm) in enumerate(raised):
        widest = max(1.0, *(s for s, _ in profile))  # onions bulge out
        reach = affinity.scale(poly, widest, widest, origin=poly.centroid)
        if taken.intersection(reach).area > min_area_mm2:
            rest = shapely.make_valid(poly.difference(taken))
            footprints.extend((p, eave + roof_mm) for p in _polygons(rest))
            continue
        # what could stand on it: smaller flat footprints, and smaller shaped
        # ones by their eave, the lowest point of their top
        standing = [(p, h) for p, h in footprints] + [
            (p, e) for p, e, _, _ in raised[n + 1 :]
        ]
        cut = _lantern_hole(poly, eave, profile, roof_mm, standing, simplify_mm)
        if cut is None:
            solids.append((poly, eave, profile, roof_mm, None))
            taken = taken.union(reach)
        else:
            hole, kept, hole_top = cut
            solids.append(
                (Polygon(poly.exterior, [hole.exterior]), eave, kept, roof_mm, hole_top)
            )
            taken = taken.union(reach.difference(hole))
    if not taken.is_empty:
        footprints = [
            (p, h)
            for fp, h in footprints
            for p in _polygons(shapely.make_valid(fp.difference(taken)))
        ]
    solids += [(p, h, (), 0.0, None) for p, h in resolve_overlaps(footprints)]

    for poly, h_mm, profile, roof_mm, hole_top in solids:
        if poly.area < min_area_mm2:
            continue
        ring_pts = np.vstack(
            [np.asarray(poly.exterior.coords)]
            + [np.asarray(r.coords) for r in poly.interiors]
        )
        ground = _sample(relief, width_mm, depth_mm, ring_pts) + base_mm
        prism = _prism(
            poly,
            float(ground.min()) - sink_mm,
            float(ground.max()) + h_mm,
            profile,
            roof_mm,
            None if hole_top is None else float(ground.max()) + hole_top,
        )
        if prism is None:
            continue
        v, r, w = prism
        verts.append(v)
        roofs.append(r + offset)
        walls.append(w + offset)
        offset += v.shape[0]
        count += 1

    for solid in meshes:
        v, f = solid(min_feature_mm / mm_per_m)
        xy = to_model(v[:, :2])
        on_ground = v[:, 2] <= 1e-9
        z0 = float(_sample(relief, width_mm, depth_mm, xy[on_ground]).min())
        z = z0 + base_mm - sink_mm + v[:, 2] * mm_per_m * scale
        v = np.column_stack([xy, z])
        p0, p1, p2 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
        up = np.cross(p1 - p0, p2 - p0)[:, 2] > 0
        verts.append(v)
        roofs.append(f[up] + offset)
        walls.append(f[~up] + offset)
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


def _star_shaped(poly: Polygon) -> bool:
    """True when ``poly`` has no holes and sees every vertex from its centroid,
    so scaling its ring toward the centroid stays inside it."""
    if poly.interiors:
        return False
    c = poly.centroid
    grown = poly.buffer(1e-6)
    return all(
        grown.covers(LineString([c, p])) for p in list(poly.exterior.coords)[:-1]
    )


def _lantern_hole(
    poly: Polygon,
    eave: float,
    profile: Profile,
    roof_mm: float,
    standing: list[tuple[Polygon, float]],
    margin: float,
) -> tuple[Polygon, Profile, float] | None:
    """Where a shaped roof makes room for what stands on it.

    Footprints less than half the roof's size, inside it and rising above
    its eave, such as a lantern, a finial or a statue on a dome, would pass
    through the roof. Instead the roof stops at the highest ring that still
    encloses them, a flat ring joins it to their outline, and they stand in
    the hole that leaves, from the ground up, touching the roof only along
    the hole's walls. Those that stay below that ring are dropped from the
    hole and end up inside the roof.

    :param poly: the roof's footprint, star-shaped, no holes.
    :param eave: eave height above the ground.
    :param profile: the roof's rings, ending at the apex.
    :param roof_mm: roof height.
    :param standing: (footprint, lowest top) of everything that could
        stand on it.
    :param margin: clearance between the hole and the ring that encloses it.
    :return: (hole, the profile up to that ring, the ring's height above
        the ground), or None when nothing stands on it or the hole is not a
        single outline.
    """
    inner = poly.buffer(-margin)
    members = [
        (p, h)
        for p, h in standing
        if p.area < 0.5 * poly.area and h > eave and inner.covers(p)
    ]
    # finer rings than the profile's, so the cut can sit close to the hole
    steps: list[tuple[float, float]] = []
    prev = (1.0, 0.0)
    for point in profile:
        for i in range(1, 9):
            t = i / 8
            steps.append(
                (prev[0] + (point[0] - prev[0]) * t, prev[1] + (point[1] - prev[1]) * t)
            )
        prev = point
    c = poly.centroid
    while members:
        hole = unary_union([p for p, _ in members])
        if not isinstance(hole, Polygon) or hole.interiors:
            return None
        last = -1
        for i, (scale, _) in enumerate(steps):
            ring = affinity.scale(poly, scale, scale, origin=c)
            if scale > 1e-9 and ring.buffer(-margin).contains(hole):
                last = i
        top = eave + (steps[last][1] * roof_mm if last >= 0 else 0.0)
        low = [m for m in members if m[1] < top - 1e-6]
        if not low:
            return orient(hole, sign=-1.0), tuple(steps[: last + 1]), top
        members = [m for m in members if m[1] >= top - 1e-6]
    return None


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
