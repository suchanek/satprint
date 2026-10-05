"""OpenStreetMap web services: OpenFreeMap vector tiles and Overpass (building
data), and Nominatim (search).

Both are free public services with usage policies: identify the client with
a User-Agent, cache what you fetch, and keep Nominatim to one request per
second. See https://operations.osmfoundation.org/policies/nominatim/ and
https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .buildings import (
    ROOF_PROFILES,
    Building,
    apply_shapes,
    buildings_from_osm,
    buildings_from_vector_tiles,
)
from .landmarks import LANDMARKS, apply_landmarks
from .terrain import BBox, Progress, _tile_range

USER_AGENT = "satprint/0.1 (+terrain relief models)"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",  # slow; last
)
# OpenFreeMap serves OSM as OpenMapTiles vector tiles from a CDN, no key.
# The TileJSON names the current weekly build's tile URL.
OPENFREEMAP_TILEJSON = "https://tiles.openfreemap.org/planet"
VECTOR_ZOOM = 14  # highest zoom; buildings carry render_height there
VECTOR_WORKERS = 8
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_ATTRIBUTION = "Buildings (c) OpenStreetMap contributors, ODbL"
OVERTURE_ATTRIBUTION = (
    "Buildings (c) Overture Maps Foundation and OpenStreetMap contributors, ODbL"
)


TILE_DEG = 0.01  # building tiles: ~1.1 km north-south, a few seconds each
TILE_WORKERS = 2  # overpass-api.de allows two concurrent queries per address
TRIES_PER_SERVER = 3  # for busy answers (429, 5xx, HTML error page)
DEAD_SERVER_S = 600.0  # skip a server that timed out or refused, this long


def buildings_query(bbox: BBox, timeout_s: int = 40) -> str:
    """Overpass QL for building outlines and building parts in ``bbox``."""
    b = f"({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    return (
        f"[out:json][timeout:{timeout_s}];("
        f'way["building"]{b};'
        f'relation["building"]["type"="multipolygon"]{b};'
        f'way["building:part"]{b};'
        f'relation["building:part"]["type"="multipolygon"]{b};'
        ");out body geom qt;"
    )


def shapes_query(bbox: BBox, timeout_s: int = 40) -> str:
    """Overpass QL for buildings with a shaped roof or a landmark entry.

    The vector tiles carry no roof tags, so this small query supplies them.
    """
    b = f"({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    shapes = "|".join(ROOF_PROFILES)
    ids = "|".join(lm.wikidata for lm in LANDMARKS)
    sets = "".join(
        f'{kind}["{key}"]["roof:shape"~"^({shapes})$"]{b};'
        f'{kind}["{key}"]["wikidata"~"^({ids})$"]{b};'
        for kind in ("way", "relation")
        for key in ("building", "building:part")
    )
    return f"[out:json][timeout:{timeout_s}];({sets});out body geom qt;"


def grid_tiles(bbox: BBox, deg: float = TILE_DEG) -> list[tuple[int, int, BBox]]:
    """Fixed-grid tiles covering ``bbox`` as (row, col, tile bbox).

    The grid is global, so overlapping areas share tiles and their cache.
    """
    r0, r1 = math.floor(bbox.south / deg), math.ceil(bbox.north / deg)
    c0, c1 = math.floor(bbox.west / deg), math.ceil(bbox.east / deg)
    return [
        (
            r,
            c,
            BBox(
                round(r * deg, 6),
                round(c * deg, 6),
                round((r + 1) * deg, 6),
                round((c + 1) * deg, 6),
            ),
        )
        for r in range(r0, max(r1, r0 + 1))
        for c in range(c0, max(c1, c0 + 1))
    ]


class OverpassClient:
    """Run Overpass queries with mirror fallback and a gzipped disk cache.

    Building downloads are split into fixed-grid tiles, each cached on its
    own, so a failed run keeps the tiles it finished and a retry or an
    overlapping area fetches only what is missing.
    """

    def __init__(
        self,
        cache_dir: str | None = None,
        session: requests.Session | None = None,
        urls: tuple[str, ...] = OVERPASS_URLS,
        timeout: float = 45.0,
    ):
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "satprint", "osm"
        )
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.urls = urls
        self.timeout = timeout
        self._preferred = 0  # index of the server that last answered
        self._dead_until: dict[str, float] = {}
        self._sleep = time.sleep

    def _post(self, url: str, query: str) -> dict:
        resp = self.session.post(url, data={"data": query}, timeout=self.timeout)
        resp.raise_for_status()
        # An overloaded server answers 200 with an HTML error page.
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"{url} returned a non-JSON response") from exc
        if "elements" not in data:
            raise RuntimeError(
                f"{url}: {data.get('remark', 'no elements in response')}"
            )
        return data

    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.json.gz")

    def _try_server(self, url: str, query: str, errors: list[str]) -> dict | None:
        """Up to ``TRIES_PER_SERVER`` attempts, backing off on busy answers.

        A server that times out or refuses the connection is marked dead
        for ``DEAD_SERVER_S`` instead, so it costs one timeout, not one per
        tile.
        """
        for attempt in range(TRIES_PER_SERVER):
            if attempt:
                self._sleep(2.0**attempt)
            try:
                return self._post(url, query)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._dead_until[url] = time.monotonic() + DEAD_SERVER_S
                errors.append(str(exc))
                return None
            except Exception as exc:  # busy: 429, 504, an HTML error page
                errors.append(str(exc))
        return None

    def query(self, query: str, cache_key: str | None = None) -> dict:
        """Run ``query``, trying the last server that answered first."""
        path = self._cache_path(cache_key or hashlib.sha1(query.encode()).hexdigest())
        if os.path.exists(path):
            with gzip.open(path, "rt") as fh:
                return json.load(fh)
        errors: list[str] = []
        n = len(self.urls)
        for k in range(n):
            i = (self._preferred + k) % n
            url = self.urls[i]
            if self._dead_until.get(url, 0.0) > time.monotonic():
                continue
            data = self._try_server(url, query, errors)
            if data is None:
                continue
            self._preferred = i
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.part"
            with gzip.open(tmp, "wt") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
            return data
        if not errors:
            errors.append("every server failed recently and is being skipped")
        # The last few errors are enough to say why; all of them is noise.
        raise RuntimeError("all Overpass servers failed: " + "; ".join(errors[-3:]))

    def shaped(self, bbox: BBox) -> dict:
        """Buildings in ``bbox`` with a shaped roof or a landmark entry."""
        return self.query(shapes_query(bbox))

    def buildings(self, bbox: BBox, progress: Progress | None = None) -> dict:
        """Building elements in ``bbox``, fetched tile by tile.

        :param bbox: area to cover.
        :param progress: called as ``progress("buildings", done, total)``.
        :return: an Overpass response with the tiles' elements, deduplicated.
        """
        tiles = grid_tiles(bbox)
        total = len(tiles)
        done = 0
        lock = threading.Lock()

        def fetch(tile: tuple[int, int, BBox]) -> dict:
            nonlocal done
            r, c, tb = tile
            data = self.query(
                buildings_query(tb), cache_key=f"tiles/{TILE_DEG}/{r}_{c}"
            )
            with lock:
                done += 1
                if progress:
                    progress("buildings", done, total)
            return data

        if progress:
            progress("buildings", 0, total)
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
            for fut in [pool.submit(fetch, t) for t in tiles]:
                try:
                    results.append(fut.result())
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise RuntimeError(
                f"{len(errors)} of {total} building tiles failed ({errors[0]}); "
                f"the other {total - len(errors)} are cached, so trying again "
                "fetches only the rest"
            )
        seen, elements = set(), []
        for data in results:
            for el in data["elements"]:
                key = (el.get("type"), el.get("id"))
                if el.get("id") is None or key not in seen:
                    seen.add(key)
                    elements.append(el)
        return {"elements": elements}


class VectorTileClient:
    """Fetch and disk-cache OpenFreeMap vector tiles covering an area.

    Tiles are cached by z/x/y without the build date, like the elevation and
    imagery tiles, so cached buildings stay until the cache is cleared.
    """

    def __init__(
        self,
        cache_dir: str | None = None,
        session: requests.Session | None = None,
        tilejson_url: str = OPENFREEMAP_TILEJSON,
        timeout: float = 30.0,
    ):
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "satprint", "vtiles"
        )
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.tilejson_url = tilejson_url
        self.timeout = timeout
        self._template: str | None = None
        self._template_at = 0.0
        self._lock = threading.Lock()

    def template(self) -> str:
        """Tile URL template of the current build, re-read once a day."""
        with self._lock:
            if self._template is None or time.monotonic() - self._template_at > 86400:
                resp = self.session.get(self.tilejson_url, timeout=self.timeout)
                resp.raise_for_status()
                self._template = resp.json()["tiles"][0]
                self._template_at = time.monotonic()
            return self._template

    def fetch(self, z: int, x: int, y: int) -> bytes:
        path = os.path.join(self.cache_dir, str(z), str(x), f"{y}.pbf")
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
        url = self.template().format(z=z, x=x, y=y)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.part"
        with open(tmp, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp, path)
        return resp.content

    def tiles(
        self,
        bbox: BBox,
        progress: Progress | None = None,
        zoom: int = VECTOR_ZOOM,
        stage: str = "buildings",
    ) -> list[tuple[int, int, int, bytes]]:
        """(z, x, y, tile bytes) for every tile covering ``bbox``.

        :param bbox: area to cover.
        :param progress: called as ``progress(stage, done, total)``.
        :param zoom: tile zoom.
        :param stage: stage name to report.
        :return: the tiles, in row order.
        """
        tx0, ty0, tx1, ty1 = _tile_range(bbox, zoom)
        coords = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        out: list = [None] * len(coords)
        if progress:
            progress(stage, 0, len(coords))
        with ThreadPoolExecutor(max_workers=min(VECTOR_WORKERS, len(coords))) as pool:
            futures = {
                pool.submit(self.fetch, zoom, tx, ty): i
                for i, (tx, ty) in enumerate(coords)
            }
            for done, fut in enumerate(as_completed(futures), 1):
                i = futures[fut]
                out[i] = (zoom, coords[i][0], coords[i][1], fut.result())
                if progress:
                    progress(stage, done, len(coords))
        return out


class Geocoder:
    """Place search through Nominatim, at most one request per second."""

    min_interval_s = 1.0

    def __init__(
        self,
        session: requests.Session | None = None,
        url: str = NOMINATIM_URL,
        timeout: float = 20.0,
        cache_size: int = 256,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.url = url
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()
        self._cache_size = cache_size

    def _fetch(self, q: str, limit: int) -> list[dict]:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.get(
                    self.url,
                    params={"q": q, "format": "jsonv2", "limit": limit},
                    headers={"Accept-Language": "en"},
                    timeout=self.timeout,
                )
            finally:
                self._last = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def search(self, q: str, limit: int = 8) -> list[dict]:
        """Matching places, best first, each with a ``bbox`` of [S, W, N, E]."""
        key = f"{limit}:{q.strip().lower()}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        out = []
        for r in self._fetch(q.strip(), limit):
            s, n, w, e = (float(v) for v in r["boundingbox"])
            out.append(
                {
                    "name": r.get("name") or r["display_name"].split(",")[0],
                    "display_name": r["display_name"],
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "bbox": [s, w, n, e],
                    "category": r.get("category", r.get("class", "")),
                    "type": r.get("type", ""),
                }
            )
        self._cache[key] = out
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return out


def fetch_buildings(
    source: str,
    bbox: BBox,
    osm: OverpassClient,
    vtiles: VectorTileClient,
    progress: Progress | None = None,
) -> tuple[list[Building], str | None]:
    """Buildings in ``bbox`` from one source, with roof shapes and landmarks.

    :param source: "openfreemap", "overpass" or "overture".
    :param bbox: area to cover.
    :param osm: Overpass client; also supplies roof shapes for OpenFreeMap.
    :param vtiles: OpenFreeMap vector tile client.
    :param progress: passed to the download.
    :return: (buildings, warning); the warning says when the roof shapes
        could not be fetched and the roofs are flat.
    """
    warning = None
    if source == "overpass":
        found = buildings_from_osm(osm.buildings(bbox, progress=progress))
    elif source == "overture":
        from .overture import buildings_from_overture

        found = buildings_from_overture(bbox, progress=progress)
    else:
        found = buildings_from_vector_tiles(vtiles.tiles(bbox, progress=progress))
        try:
            found = apply_shapes(found, buildings_from_osm(osm.shaped(bbox)))
        except Exception as exc:  # shapes refine the buildings; keep them flat
            warning = f"roof shapes unavailable, roofs are flat: {exc}"
    return apply_landmarks(found, bbox), warning
