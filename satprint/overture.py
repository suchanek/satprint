"""Overture Maps buildings: OSM merged with Microsoft and Google footprints.

Needs the ``overture`` extra (``pip install "satprint[overture]"``), which
brings the ``overturemaps`` client and pyarrow. Overture's STAC index picks
the GeoParquet files on S3 that cover the area, so a query reads a few files,
not the whole dataset, in seconds; results are cached per Overture release
under ``~/.cache/satprint/overture``.

Overture keeps the OSM tags satprint uses as columns: ``height``,
``num_floors``, ``roof_shape`` and ``roof_height``. Building parts replace
their building's outline, as with OSM.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re

import shapely
from shapely import Polygon

from .buildings import (
    DEFAULT_HEIGHT_M,
    LEVEL_HEIGHT_M,
    Building,
    _polygons,
    roof_shape,
)
from .terrain import BBox, Progress

_COLUMNS = (
    "id",
    "building_id",
    "has_parts",
    "is_underground",
    "height",
    "num_floors",
    "roof_shape",
    "roof_height",
    "sources",
)
_OSM_RECORD = re.compile(r"^([nwr])(\d+)")
_OSM_KIND = {"n": "node", "w": "way", "r": "relation"}


def _client():
    try:
        from overturemaps import core
    except ImportError as exc:
        raise RuntimeError(
            'the Overture source needs the overture extra: pip install "satprint[overture]"'
        ) from exc
    return core


def _osm_id(sources: list[dict] | None) -> str:
    """ "way/123" from an Overture ``sources`` entry for OSM, else ""."""
    for s in sources or []:
        if s.get("dataset") == "OpenStreetMap":
            if m := _OSM_RECORD.match(s.get("record_id") or ""):
                return f"{_OSM_KIND[m.group(1)]}/{m.group(2)}"
    return ""


def fetch_rows(
    bbox: BBox,
    cache_dir: str | None = None,
    release: str | None = None,
    progress: Progress | None = None,
) -> dict[str, list[dict]]:
    """Overture ``building`` and ``building_part`` rows in ``bbox``.

    :param bbox: area to cover.
    :param cache_dir: where results are cached, per release.
    :param release: an Overture release such as "2026-09-23.1"; the latest
        when None.
    :param progress: called as ``progress("overture buildings", done, 2)``.
    :return: {"building": rows, "building_part": rows}, geometry as WKB hex.
    """
    core = _client()
    release = release or core.get_latest_release()
    cache_dir = cache_dir or os.path.join(
        os.path.expanduser("~"), ".cache", "satprint", "overture"
    )
    key = hashlib.sha1(
        repr((bbox.south, bbox.west, bbox.north, bbox.east)).encode()
    ).hexdigest()
    path = os.path.join(cache_dir, release, f"{key}.json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as fh:
            return json.load(fh)

    out: dict[str, list[dict]] = {}
    box = (bbox.west, bbox.south, bbox.east, bbox.north)
    for done, kind in enumerate(("building", "building_part")):
        if progress:
            progress("overture buildings", done, 2)
        # Without the STAC index the client scans every file of the
        # release: about a minute and 4 GB of memory for one city block.
        reader = core.record_batch_reader(kind, box, release=release, stac=True)
        rows = []
        # None: no file covers the area
        for row in reader.read_all().to_pylist() if reader is not None else []:
            keep = {k: row.get(k) for k in _COLUMNS}
            keep["geometry"] = bytes(row["geometry"]).hex()
            rows.append(keep)
        out[kind] = rows
    if progress:
        progress("overture buildings", 2, 2)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.part"
    with gzip.open(tmp, "wt") as fh:
        json.dump(out, fh)
    os.replace(tmp, path)
    return out


def buildings_from_rows(rows: dict[str, list[dict]]) -> list[Building]:
    """Parse :func:`fetch_rows` output into buildings."""
    parts = rows.get("building_part", [])
    with_parts = {p.get("building_id") for p in parts}
    out: list[Building] = []
    for kind in ("building", "building_part"):
        for row in rows.get(kind, []):
            if row.get("is_underground"):
                continue
            if kind == "building" and row.get("id") in with_parts:
                continue  # drawn by its parts
            height = row.get("height")
            if height is None:
                floors = row.get("num_floors")
                height = (
                    max(1.0, float(floors)) * LEVEL_HEIGHT_M
                    if floors
                    else DEFAULT_HEIGHT_M
                )
            geom = shapely.make_valid(shapely.from_wkb(bytes.fromhex(row["geometry"])))
            for poly in _polygons(geom):
                if not isinstance(poly, Polygon) or poly.area <= 0:
                    continue
                profile, roof_h = roof_shape(
                    row.get("roof_shape"), row.get("roof_height"), float(height), poly
                )
                out.append(
                    Building(
                        poly,
                        float(height),
                        kind == "building_part",
                        profile,
                        roof_h,
                        osm_id=_osm_id(row.get("sources")),
                    )
                )
    return out


def buildings_from_overture(
    bbox: BBox, progress: Progress | None = None
) -> list[Building]:
    """Overture buildings in ``bbox``, cached per release."""
    return buildings_from_rows(fetch_rows(bbox, progress=progress))
