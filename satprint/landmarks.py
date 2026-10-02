"""Exact shapes for landmarks that OSM's roof tags cannot describe.

A landmark is matched by its Wikidata ID, or by its OSM element where the
source carries no tags (Overture keeps the OSM ID in its sources). The
replacement is built from published dimensions and centered on the matched
footprint, which may be drawn larger or smaller than the real building.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

from shapely import Polygon

from .buildings import Building, Profile


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


def apply_landmarks(buildings: list[Building]) -> list[Building]:
    """Replace each building matching a :data:`LANDMARKS` entry by its shape."""
    by_wikidata = {lm.wikidata: lm for lm in LANDMARKS}
    by_osm = {lm.osm_id: lm for lm in LANDMARKS}
    out = []
    for b in buildings:
        lm = by_wikidata.get(b.wikidata) or by_osm.get(b.osm_id)
        out.append(lm.build(b) if lm else b)
    return out
