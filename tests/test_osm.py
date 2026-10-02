import gzip
import json

import pytest
import requests

from satprint.osm import TILE_DEG, Geocoder, OverpassClient, buildings_query, grid_tiles
from satprint.terrain import BBox


class FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.headers = {"User-Agent": "python-requests/0"}
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_buildings_query_covers_outlines_and_parts():
    q = buildings_query(BBox(1, 2, 3, 4))
    assert "(1,2,3,4)" in q and "out body geom" in q
    assert q.count('"building:part"') == 2 and q.count('["building"]') == 2


def _client(tmp_path, session, urls):
    client = OverpassClient(cache_dir=str(tmp_path), session=session, urls=urls)
    client._sleep = lambda s: None
    return client


def test_overpass_retries_busy_server_then_caches(tmp_path):
    good = {"elements": [{"type": "way", "id": 1}]}
    session = FakeSession(
        [
            FakeResponse(200, text="<html>overloaded</html>"),  # 200 but HTML
            FakeResponse(504),
            FakeResponse(200, good),
        ]
    )
    client = _client(tmp_path, session, ("a", "b"))
    assert session.headers["User-Agent"].startswith("satprint")
    assert client.query("q") == good
    assert session.calls == ["a", "a", "a"]
    assert client.query("q") == good  # served from disk
    assert len(session.calls) == 3


def test_overpass_moves_on_after_busy_retries(tmp_path):
    good = {"elements": []}
    session = FakeSession([FakeResponse(429)] * 3 + [FakeResponse(200, good)])
    client = _client(tmp_path, session, ("a", "b"))
    assert client.query("q") == good
    assert session.calls == ["a", "a", "a", "b"]


def test_overpass_skips_a_server_that_timed_out(tmp_path):
    good = {"elements": []}
    session = FakeSession(
        [
            requests.Timeout("read timed out"),
            FakeResponse(200, good),
            FakeResponse(200, good),
        ]
    )
    client = _client(tmp_path, session, ("slow", "fast"))
    client.query("q1")
    client._preferred = 0  # even when asked to start with it again
    client.query("q2")
    assert session.calls == ["slow", "fast", "fast"]


def test_overpass_reports_failures(tmp_path):
    session = FakeSession(
        [FakeResponse(500)] * 3 + [requests.ConnectionError("refused")]
    )
    client = _client(tmp_path, session, ("a", "b"))
    with pytest.raises(RuntimeError, match="500.*refused"):
        client.query("q")


def test_geocoder_parses_and_caches():
    hit = {
        "name": "Eiffel Tower",
        "display_name": "Eiffel Tower, Paris, France",
        "lat": "48.858",
        "lon": "2.294",
        "boundingbox": ["48.857", "48.859", "2.293", "2.296"],
        "category": "tourism",
        "type": "attraction",
    }
    session = FakeSession([FakeResponse(200, [hit])])
    geo = Geocoder(session=session)
    geo.min_interval_s = 0
    out = geo.search("  Eiffel Tower ")
    assert out == [
        {
            "name": "Eiffel Tower",
            "display_name": "Eiffel Tower, Paris, France",
            "lat": 48.858,
            "lon": 2.294,
            "bbox": [48.857, 2.293, 48.859, 2.296],
            "category": "tourism",
            "type": "attraction",
        }
    ]
    assert session.calls[0][1]["q"] == "Eiffel Tower"
    assert geo.search("eiffel tower") == out  # cached, case-insensitive
    assert len(session.calls) == 1


def test_grid_tiles_cover_bbox_on_a_global_grid():
    bbox = BBox(40.7025, -74.0175, 40.7225, -73.9995)
    tiles = grid_tiles(bbox)
    assert len(tiles) == 3 * 3
    south = min(t.south for _, _, t in tiles)
    north = max(t.north for _, _, t in tiles)
    assert south <= bbox.south and north >= bbox.north
    for r, c, t in tiles:
        assert t.south == round(r * TILE_DEG, 6) and t.west == round(c * TILE_DEG, 6)
    # an overlapping area reuses the same tile ids
    shifted = {(r, c) for r, c, _ in grid_tiles(BBox(40.712, -74.005, 40.718, -73.995))}
    assert shifted & {(r, c) for r, c, _ in tiles}


class TileOverpass(OverpassClient):
    """Answers each tile query with one building per tile plus a shared one."""

    def __init__(self, cache_dir, fail_tiles=()):
        super().__init__(cache_dir=cache_dir, urls=("x",))
        self._sleep = lambda s: None
        self.posts = 0
        self.fail_tiles = set(fail_tiles)

    def _post(self, url, query):
        self.posts += 1
        if any(f"({s}," in query for s in self.fail_tiles):
            raise RuntimeError("504 Gateway Timeout")
        own = {"type": "way", "id": hash(query) % 10**9, "tags": {}}
        shared = {"type": "way", "id": 1, "tags": {}}  # spans a tile edge
        return {"elements": [own, shared]}


def test_building_tiles_are_cached_and_deduplicated(tmp_path):
    bbox = BBox(40.7025, -74.0175, 40.7225, -73.9995)
    client = TileOverpass(str(tmp_path))
    seen = []
    data = client.buildings(bbox, progress=lambda *a: seen.append(a))
    assert client.posts == 9
    assert len(data["elements"]) == 9 + 1  # the shared way once
    assert seen[0] == ("buildings", 0, 9) and seen[-1] == ("buildings", 9, 9)
    files = list(tmp_path.rglob("*.json.gz"))
    assert len(files) == 9
    with gzip.open(files[0], "rt") as fh:
        assert "elements" in json.load(fh)
    client.buildings(bbox)
    assert client.posts == 9  # all from disk


def test_failed_tiles_keep_the_rest_cached(tmp_path):
    bbox = BBox(40.7025, -74.0175, 40.7225, -73.9995)
    bad = grid_tiles(bbox)[0][2].south
    client = TileOverpass(str(tmp_path), fail_tiles={bad})
    with pytest.raises(
        RuntimeError, match="3 of 9 building tiles failed.*other 6 are cached"
    ):
        client.buildings(bbox)
    assert client.posts == 6 + 3 * 3  # each failed tile was tried three times
    retry = TileOverpass(str(tmp_path))
    retry.buildings(bbox)
    assert retry.posts == 3  # the failed row of tiles only


def test_overpass_prefers_the_last_server_that_answered(tmp_path):
    good = {"elements": []}
    session = FakeSession([FakeResponse(504)] * 3 + [FakeResponse(200, good)] * 2)
    client = _client(tmp_path, session, ("a", "b"))
    client.query("q1")
    client.query("q2")
    assert session.calls == ["a", "a", "a", "b", "b"]


class TileSession(FakeSession):
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/planet"):
            return FakeResponse(
                200, {"tiles": ["https://t.example/b1/{z}/{x}/{y}.pbf"]}
            )
        r = FakeResponse(200)
        r.content = url.encode()
        return r


def test_vector_tiles_fetch_cover_and_cache(tmp_path):
    from satprint.osm import VectorTileClient

    session = TileSession([])
    client = VectorTileClient(cache_dir=str(tmp_path), session=session)
    bbox = BBox(40.74, -74.0, 40.77, -73.97)
    seen = []
    tiles = client.tiles(bbox, progress=lambda *a: seen.append(a))
    n = len(tiles)
    assert n >= 4 and seen[-1] == ("buildings", n, n)
    z, x, y, data = tiles[0]
    assert z == 14 and data == f"https://t.example/b1/14/{x}/{y}.pbf".encode()
    tile_gets = [c for c in session.calls if "t.example" in c]
    assert (
        len(tile_gets) == n
        and session.calls.count("https://tiles.openfreemap.org/planet") == 1
    )
    client.tiles(bbox)
    assert len([c for c in session.calls if "t.example" in c]) == n  # from disk
