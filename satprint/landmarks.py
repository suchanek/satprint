"""Exact shapes for landmarks that OSM's roof tags cannot describe.

A :class:`Landmark` is matched by its Wikidata ID, or by its OSM element where
the source carries no tags (Overture keeps the OSM ID in its sources). The
replacement is a prism with a roof profile, built from published dimensions
and centered on the matched footprint, which may be drawn larger or smaller
than the real building.

A :class:`MeshLandmark` is for what a prism cannot express, such as an arch or
separate legs. It supplies a closed triangle mesh and is placed at a fixed
position, replacing the buildings OSM maps under it, so nothing is matched.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
from shapely import MultiPoint, Polygon, box

from .buildings import Building, Profile
from .mesh import Mesh
from .terrain import BBox


def circle(lon: float, lat: float, radius_m: float, segments: int = 96) -> Polygon:
    """A lon/lat circle of ``radius_m`` meters around (lon, lat)."""
    k_lat = 111_320.0
    k_lon = k_lat * math.cos(math.radians(lat))
    t = [2 * math.pi * i / segments for i in range(segments)]
    return Polygon(
        [
            (lon + radius_m * math.cos(a) / k_lon, lat + radius_m * math.sin(a) / k_lat)
            for a in t
        ]
    )


def truncated_sphere(
    b: Building, radius_m: float, height_m: float, rings: int = 24
) -> Building:
    """A sphere of ``radius_m`` cut by the ground so it stands ``height_m``
    tall, centered on ``b``'s footprint.

    :param b: the matched building; its footprint gives the center.
    :param radius_m: sphere radius.
    :param height_m: height above the ground, at most twice the radius.
    :param rings: rings from the ground to the top.
    """
    zc = height_m - radius_m  # center above the ground
    base_r = math.sqrt(radius_m**2 - zc**2)
    t0 = math.asin(-zc / radius_m)
    profile: Profile = tuple(
        (
            radius_m * math.cos(t) / base_r,
            (zc + radius_m * math.sin(t)) / height_m,
        )
        for t in (t0 + (math.pi / 2 - t0) * i / rings for i in range(1, rings + 1))
    )
    c = b.footprint.centroid
    return replace(
        b,
        footprint=circle(c.x, c.y, base_r),
        height_m=height_m,
        profile=profile,
        roof_height_m=height_m,
    )


@dataclass(frozen=True)
class Landmark:
    """A known building, matched by Wikidata or OSM id, and the rule that reshapes it."""

    name: str
    wikidata: str
    osm_id: str
    build: Callable[[Building], Building]


LANDMARKS: tuple[Landmark, ...] = (
    # 157 m wide and 112 m tall (Populous, 2023).
    Landmark(
        "Sphere, Las Vegas",
        "Q60749353",
        "way/976405284",
        lambda b: truncated_sphere(b, radius_m=78.5, height_m=112.0),
    ),
)


def _loft(rings: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Closed solid through consecutive rings of equal vertex count, each end
    capped by a fan around the ring's mean. Rings may be given either way
    round; the result is outward-wound. Consecutive rings must differ."""
    k = rings[0].shape[0]
    n = len(rings)
    ends = [rings[0].mean(axis=0), rings[-1].mean(axis=0)]
    vertices = np.vstack(rings + [np.array(ends)])
    j = np.arange(k)
    nxt = (j + 1) % k
    faces = []
    for i in range(n - 1):
        a, b, c, d = i * k + j, i * k + nxt, (i + 1) * k + nxt, (i + 1) * k + j
        faces += [np.column_stack([a, b, c]), np.column_stack([a, c, d])]
    first, last = j, (n - 1) * k + j
    faces.append(np.column_stack([np.full(k, n * k), first[nxt], first]))
    faces.append(np.column_stack([np.full(k, n * k + 1), last, last[nxt]]))
    faces = np.vstack(faces)
    if Mesh(vertices, faces).volume_mm3() < 0:
        faces = faces[:, ::-1]
    return vertices, faces


def _join(*solids: tuple[np.ndarray, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Concatenate solids into one vertex and face array."""
    offsets = np.cumsum([0] + [v.shape[0] for v, _ in solids[:-1]])
    return (
        np.vstack([v for v, _ in solids]),
        np.vstack([f + o for (_, f), o in zip(solids, offsets, strict=True)]),
    )


def _stack(profile: list[tuple[float, float]], ring) -> list[np.ndarray]:
    """``ring(z, size)`` for each (z, size) of ``profile``, dropping repeats."""
    out: list[np.ndarray] = []
    for z, size in profile:
        r = ring(z, size)
        if not out or not np.array_equal(r, out[-1]):
            out.append(r)
    return out


def _arch(min_feature_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Gateway Arch, St. Louis, in the plane y = 0 with x along the span.

    Published (National Park Service): 630 ft tall and wide, the weighted
    catenary below for the centroid line, and an equilateral-triangle section
    with a flat outer face, 54 ft a side at the base and 17 ft at the top.
    Estimated: the section tapers linearly with height. The ends are cut flat
    at the ground.

    :param min_feature_m: shortest side of the section.
    """
    ft = 0.3048
    x_end = 299.2239

    def centroid(x):
        return (693.8597 - 68.7672 * np.cosh(0.0100333 * x)) * ft

    # rings evenly spaced along the curve, running past the ground at both ends
    xf = np.linspace(-1.05 * x_end, 1.05 * x_end, 40001) * ft
    zf = centroid(xf / ft)
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xf), np.diff(zf)))])
    x = np.interp(np.linspace(0, arc[-1], 241), arc, xf)
    z = centroid(x / ft)
    slope = np.gradient(z, x)
    tangent = np.column_stack([np.ones_like(x), slope])
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])  # outward, up
    top = centroid(0.0)
    side = np.maximum(
        (54.0 + (17.0 - 54.0) * np.clip(z / top, 0, 1)) * ft, min_feature_m
    )
    r = side / (2 * math.sqrt(3))  # inradius; the flat face is r out
    rings = []
    for i in range(x.size):
        pts = [(r[i], -side[i] / 2), (r[i], side[i] / 2), (-2 * r[i], 0.0)]
        rings.append(
            np.array(
                [(x[i] + u * normal[i, 0], w, z[i] + u * normal[i, 1]) for u, w in pts]
            )
        )
    below = np.array([ring[:, 2].max() <= 0 for ring in rings])
    half = x.size // 2
    lo = int(np.flatnonzero(below[:half])[-1])
    hi = half + int(np.flatnonzero(below[half:])[0])
    rings = rings[lo : hi + 1]
    for ring in (rings[0], rings[-1]):
        ring[:, 2] = 0.0
    for ring in rings[1:-1]:
        ring[:, 2] = np.maximum(ring[:, 2], 0.0)
    return _loft(rings)


def _eiffel(min_feature_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Eiffel Tower, Paris, axes along the base's sides.

    Published: 330 m with the antenna, a 125 m square base, platforms at 57,
    116 and 276 m, about 65, 37 and 10 m across there. Estimated: the curve
    of the outline between those points, the legs' width, the platform slabs,
    and the top section and antenna.

    Four legs follow the outline from the ground to the second platform,
    tied together by the first; above the second the tower is one solid with
    the third platform as a wider slab. Where the legs would be as wide as
    the gaps between them, the tower is one solid from the ground instead.

    :param min_feature_m: thinnest slab, leg and spire.
    """
    m = min_feature_m
    z1, z2, z3 = 57.0, 116.0, 276.0
    slab = min(max(4.0, m), 20.0)  # thicker would not leave room between platforms
    margin = 2.5  # a slab is this much wider than the tower on each side

    def half(z):  # half-width of the outline: 62.5 m at the ground, then
        # 32.5, 18.5 and 5 m at the platforms
        if z < z1:
            return 62.5 * (32.5 / 62.5) ** (z / z1)
        return 1.79 + 30.71 * math.exp(-(z - z1) / 96.93)

    def width(z):  # of a leg: 25 m at the ground, 11 m at the second platform
        return max(25.0 - 14.0 * z / z2, m)

    def square(z, h):
        h = max(h, m / 2)
        return np.array([(-h, -h, z), (h, -h, z), (h, h, z), (-h, h, z)])

    def curve(a, b):  # the outline from a up to b, a ring every 12 m or less
        return [
            (float(z), half(float(z)))
            for z in np.linspace(a, b, math.ceil((b - a) / 12) + 1)
        ]

    def platform(zp):
        s = half(zp - slab) + margin
        return [(zp - slab, s), (zp, s)]

    za, zb = z1 - slab, z2 - slab
    top = [
        *platform(z2),
        *curve(z2, z3 - slab),
        *platform(z3),
        (z3, half(z3)),
        (300.0, 3.5),
        (300.0, 1.5),
        (330.0, 0.5),
    ]
    # legs need gaps wider than themselves, at the ground and under each platform
    if any(2 * (half(z) - width(z)) <= width(z) for z in (0.0, za, zb)):
        whole = [*curve(0.0, za), *platform(z1), *curve(z1, zb), *top]
        return _loft(_stack(whole, square))
    solids = [_loft(_stack(top, square)), _loft(_stack(platform(z1), square))]
    for lo, hi in ((0.0, za), (z1, zb)):
        for sx in (-1, 1):
            for sy in (-1, 1):
                rings = []
                for z, outer in curve(lo, hi):
                    h = width(z) / 2
                    c = outer - h
                    rings.append(
                        np.array(
                            [
                                (sx * c + dx, sy * c + dy, z)
                                for dx, dy in ((-h, -h), (h, -h), (h, h), (-h, h))
                            ]
                        )
                    )
                solids.append(_loft(rings))
    return _join(*solids)


def _needle(min_feature_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Space Needle, Seattle, a surface of revolution about the origin.

    Published: 184 m (605 ft) tall, the saucer 42 m (138 ft) across.
    Estimated: the radius of the hourglass stem, the height of the saucer
    and the spire.

    :param min_feature_m: smallest diameter.
    """
    segments = 64
    angle = 2 * math.pi * np.arange(segments) / segments

    def ring(z, r):
        r = max(r, min_feature_m / 2)
        return np.column_stack(
            [r * np.cos(angle), r * np.sin(angle), np.full(segments, z)]
        )

    # (height, radius): hourglass stem, saucer underside, rim, roof, spire
    profile = [
        (0.0, 18.0),
        (15.0, 12.5),
        (40.0, 8.0),
        (75.0, 5.5),
        (110.0, 5.0),
        (125.0, 5.6),
        (138.0, 7.5),
        (146.0, 10.0),
        (150.0, 17.0),
        (154.0, 21.0),
        (158.0, 21.0),
        (162.0, 17.0),
        (165.0, 9.0),
        (167.0, 3.5),
        (184.0, 0.5),
    ]
    return _loft(_stack(profile, ring))


def _christ(min_feature_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Christ the Redeemer, Rio de Janeiro, arms along x.

    Published: a 30 m statue on an 8 m pedestal, 28 m from hand to hand.
    Estimated: every section of the figure.

    Thickening a figure this small would leave a lump, so where the arms are
    thinner than ``min_feature_m`` the whole statue is enlarged instead, in
    proportion, until they are not. On a city-sized print it stands several
    times its true size, as a symbol on a map does.

    :param min_feature_m: least thickness of the arms.
    """
    arm = 2.8  # thickness at the shoulder

    def ring(z, hx, hy):  # a rectangle with its corners cut
        c = 0.3 * min(hx, hy)
        return np.array(
            [
                (hx, c - hy, z),
                (hx, hy - c, z),
                (hx - c, hy, z),
                (c - hx, hy, z),
                (-hx, hy - c, z),
                (-hx, c - hy, z),
                (c - hx, -hy, z),
                (hx - c, -hy, z),
            ]
        )

    # (height, half-width, half-depth): pedestal, robe, shoulders, neck, head.
    # The sides are flat from 29.5 to 33 m, where the arms meet them.
    figure = [
        (0.0, 4.5, 4.5),
        (8.0, 4.5, 4.5),
        (8.0, 3.2, 2.6),
        (20.0, 2.8, 2.2),
        (28.0, 3.2, 2.0),
        (29.5, 3.5, 2.0),
        (33.0, 3.5, 2.0),
        (33.5, 1.3, 1.3),
        (34.2, 1.6, 1.7),
        (36.5, 1.7, 1.8),
        (38.0, 0.9, 1.0),
    ]
    solids = [_loft([ring(*r) for r in figure])]
    for side in (-1, 1):
        ends = []
        for x, z0, z1, hy in ((3.5, 30.0, 30.0 + arm, 1.2), (14.0, 31.0, 32.6, 0.8)):
            ends.append(
                np.array(
                    [
                        (side * x, -hy, z0),
                        (side * x, hy, z0),
                        (side * x, hy, z1),
                        (side * x, -hy, z1),
                    ]
                )
            )
        solids.append(_loft(ends))
    v, f = _join(*solids)
    return v * max(1.0, min_feature_m / arm), f


@dataclass(frozen=True)
class MeshLandmark:
    """A structure built as a mesh from code and placed at a fixed position."""

    name: str
    lon: float
    lat: float
    bearing: float  # degrees clockwise from north of the local x axis
    # min_feature_m -> (vertices, faces); vertices in meters, x east, y north,
    # z up, before rotation by the bearing
    build: Callable[[float], tuple[np.ndarray, np.ndarray]]

    def solid(self, min_feature_m: float) -> tuple[np.ndarray, np.ndarray]:
        """(vertices, faces) as a :attr:`Building.solid` returns them: lon, lat
        and meters above the ground."""
        v, faces = self.build(min_feature_m)
        k_lat = 111_320.0
        k_lon = k_lat * math.cos(math.radians(self.lat))
        b = math.radians(self.bearing)
        east = v[:, 0] * math.sin(b) - v[:, 1] * math.cos(b)
        north = v[:, 0] * math.cos(b) + v[:, 1] * math.sin(b)
        lonlat = np.column_stack(
            [self.lon + east / k_lon, self.lat + north / k_lat, v[:, 2]]
        )
        return lonlat, faces


# Positions are the centroid and long axis (the Arch) or side direction (the
# Eiffel Tower) of the OSM building:part footprints in the OpenFreeMap tiles,
# measured with the minimum rotated rectangle of their union. The Needle is
# round.
MESH_LANDMARKS: tuple[MeshLandmark, ...] = (
    MeshLandmark("Gateway Arch, St. Louis", -90.184935, 38.624697, 17.95, _arch),
    # True proportions at every scale: thickened, the tower loses its outline.
    MeshLandmark(
        "Eiffel Tower, Paris", 2.294494, 48.858262, 44.2, lambda m: _eiffel(0.0)
    ),
    MeshLandmark("Space Needle, Seattle", -122.349306, 47.620506, 0.0, _needle),
    # OSM maps the statue as a node (node/4919986733), with no outline. It
    # faces about east, so its arms run north-south.
    MeshLandmark("Christ the Redeemer, Rio", -43.2104585, -22.9519173, 0.0, _christ),
)


def apply_landmarks(buildings: list[Building], bbox: BBox) -> list[Building]:
    """Replace each building matching a :data:`LANDMARKS` entry by its shape,
    and put each :data:`MESH_LANDMARKS` entry in place of the buildings under it.

    A mesh landmark is added only when its whole plan outline lies inside
    ``bbox``; otherwise the buildings from the source stay.

    :param buildings: buildings from any source.
    :param bbox: area the buildings were fetched for.
    """
    by_wikidata = {lm.wikidata: lm for lm in LANDMARKS}
    by_osm = {lm.osm_id: lm for lm in LANDMARKS}
    out = []
    for b in buildings:
        lm = by_wikidata.get(b.wikidata) or by_osm.get(b.osm_id)
        out.append(lm.build(b) if lm else b)
    area = box(bbox.west, bbox.south, bbox.east, bbox.north)
    for mlm in MESH_LANDMARKS:
        v, _ = mlm.solid(0.0)
        footprint = MultiPoint(v[:, :2]).convex_hull
        if not area.contains(footprint):
            continue
        under = footprint.buffer(5.0 / 111_320.0)
        out = [b for b in out if not under.contains(b.footprint.representative_point())]
        out.append(Building(footprint, float(v[:, 2].max()), solid=mlm.solid))
    return out
