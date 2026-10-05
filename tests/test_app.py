import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from shapely import LineString, box

from satprint.app import create_app
from satprint.mesh import read_binary_stl, read_glb
from satprint.osm import Geocoder, OverpassClient, VectorTileClient
from tests.test_bridges import encode
from tests.test_buildings import encode_tile
from tests.test_terrain import FakeFetcher, FakeImageryFetcher


class FakeOverpass(OverpassClient):
    """Two buildings near the center of whatever bbox is asked for."""

    def __init__(self, fail=False):
        super().__init__(cache_dir="/nonexistent")
        self.fail = fail
        self.calls = 0

    def buildings(self, bbox, progress=None):
        self.calls += 1
        if progress:
            progress("buildings", 1, 1)
        if self.fail:
            raise RuntimeError("all Overpass servers failed: 429")
        lat, lon = bbox.mid_lat, bbox.mid_lon
        d = (bbox.north - bbox.south) / 20

        def square(la, lo):
            pts = [(la, lo), (la, lo + d), (la + d, lo + d), (la + d, lo), (la, lo)]
            return [{"lat": a, "lon": o} for a, o in pts]

        return {
            "elements": [
                {
                    "type": "way",
                    "tags": {"building": "yes", "height": "300"},
                    "geometry": square(lat, lon),
                },
                {
                    "type": "way",
                    "tags": {"building": "yes"},
                    "geometry": square(lat - 2 * d, lon - 2 * d),
                },
            ]
        }

    def shaped(self, bbox):
        """Shaped roofs for the vector tile source: none, or a failure."""
        self.shaped_calls = getattr(self, "shaped_calls", 0) + 1
        if self.fail:
            raise RuntimeError("all Overpass servers failed: 429")
        return {"elements": []}


class FakeVectorTiles(VectorTileClient):
    """One vector tile with one building at its center, or a failure; for
    bridges, a river with a road across it."""

    def __init__(self, fail=False):
        super().__init__(cache_dir="/nonexistent")
        self.fail = fail
        self.calls = 0

    def tiles(self, bbox, progress=None, zoom=14, stage="buildings"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("tiles.openfreemap.org unreachable")
        from satprint.terrain import _tile_range

        tx0, ty0, tx1, ty1 = _tile_range(bbox, zoom)
        out = []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                if stage == "water":
                    # the western half of every tile is a river
                    feats = [
                        {
                            "geometry": box(0, 0, 2048, 4096),
                            "properties": {"class": "river"},
                        }
                    ]
                    out.append((zoom, tx, ty, encode_tile(feats, layer="water")))
                    continue
                if stage == "bridges":
                    # a river across every tile and a road over it
                    out.append(
                        encode(
                            water=[box(1500, 0, 2600, 4096)],
                            lines=[(LineString([(0, 2048), (4096, 2048)]), "primary")],
                            z=zoom,
                            x=tx,
                            y=ty,
                        )
                    )
                    continue
                # one block over the whole tile, so any area gets a building
                feats = [
                    {
                        "geometry": box(0, 0, 4096, 4096),
                        "properties": {"render_height": 120},
                    }
                ]
                out.append((zoom, tx, ty, encode_tile(feats)))
        if progress:
            progress("buildings", len(out), len(out))
        return out


class FakeGeocoder(Geocoder):
    def __init__(self):
        super().__init__()
        self.min_interval_s = 0

    def _fetch(self, q, limit):
        if q == "boom":
            raise ConnectionError("nominatim down")
        return [
            {
                "display_name": f"{q}, Somewhere",
                "lat": "46.0",
                "lon": "7.6",
                "boundingbox": ["45.9", "46.1", "7.5", "7.7"],
                "category": "place",
                "type": "city",
            }
        ]


def make_client(**kw):
    deps = {
        "tile_fetcher": FakeFetcher(),
        "imagery_fetcher": FakeImageryFetcher(),
        "overpass": FakeOverpass(),
        "geocoder": FakeGeocoder(),
        # unreachable by default, so "auto" falls back to the Overpass fake
        "vector_tiles": FakeVectorTiles(fail=True),
    }
    deps.update(kw)
    return TestClient(create_app(**deps))


@pytest.fixture
def client():
    return make_client()


def test_index_and_presets(client):
    assert "satprint" in client.get("/").text
    presets = client.get("/api/presets").json()
    assert len(presets) >= 50 and all(len(p["bbox"]) == 4 for p in presets)
    for p in presets:
        s, w, n, e = p["bbox"]
        assert -85 < s < n < 85 and -180 <= w < e <= 180, p["name"]
    groups = {p["group"] for p in presets}
    assert len(groups) == 2
    assert any(p["buildings"] for p in presets) and not all(
        p["buildings"] for p in presets
    )
    assert len({p["name"] for p in presets}) == len(presets)
    assert client.get("/static/app.js").status_code == 200


def test_synthetic_model_and_download(client):
    r = client.post(
        "/api/model",
        json={
            "source": "synthetic",
            "resolution": 64,
            "width_mm": 80,
            "exaggeration": 2,
            "name": "demo peak",
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    info = j["info"]
    assert info["width_mm"] == 80 and info["cols"] == 64 and info["triangles"] > 0
    assert j["preview_png"].startswith("data:image/png;base64,")
    stl = client.get(j["stl_url"])
    assert stl.status_code == 200 and stl.headers["content-type"].startswith(
        "model/stl"
    )
    assert "demo_peak.stl" in stl.headers["content-disposition"]
    tri = read_binary_stl(stl.content)
    assert tri.shape[0] == info["triangles"]
    assert tri[..., 0].max() == pytest.approx(80, abs=1e-3)
    assert tri[..., 2].max() == pytest.approx(info["height_mm"], abs=1e-3)
    png = client.get(j["png_url"])
    assert png.headers["content-type"] == "image/png"
    assert client.get(j["stl_url"].replace(j["model_id"], "nope")).status_code == 404


def test_terrarium_model_uses_fetcher(client):
    r = client.post(
        "/api/model",
        json={
            "source": "terrarium",
            "resolution": 96,
            "bbox": {"south": 45.9, "west": 7.5, "north": 46.0, "east": 7.7},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["info"]["source_meta"]["tiles"] >= 1


def test_validation_errors(client):
    r = client.post("/api/model", json={"source": "terrarium"})
    assert r.status_code == 400
    r = client.post(
        "/api/model",
        json={
            "source": "terrarium",
            "bbox": {"south": 46, "west": 7, "north": 45, "east": 8},
        },
    )
    assert r.status_code == 400
    r = client.post("/api/model", json={"source": "synthetic", "width_mm": 5})
    assert r.status_code == 422
    r = client.post("/api/model", json={"source": "upload"})
    assert r.status_code == 400


def test_upload_flow(client):
    arr = (np.random.rand(24, 32) * 2000).astype(np.uint16)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    r = client.post(
        "/api/upload", files={"file": ("dem.png", buf.getvalue(), "image/png")}
    )
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    r = client.post(
        "/api/model",
        json={
            "source": "upload",
            "upload_id": uid,
            "ground_width_m": 3200,
            "width_mm": 64,
            "relief_mm": 10,
        },
    )
    assert r.status_code == 200, r.text
    info = r.json()["info"]
    assert info["depth_mm"] == pytest.approx(48) and info["relief_mm"] == pytest.approx(
        10
    )
    r = client.post("/api/upload", files={"file": ("x.png", b"not a png", "image/png")})
    assert r.status_code == 400


def test_terrarium_model_has_textured_glb(client):
    r = client.post(
        "/api/model",
        json={
            "source": "terrarium",
            "resolution": 64,
            "bbox": {"south": 45.8, "west": 7.4, "north": 45.9, "east": 7.6},
            "name": "alps",
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["info"]["textured"] is True and j["glb_url"].endswith("/alps.glb")
    glb = client.get(j["glb_url"])
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert "alps.glb" in glb.headers["content-disposition"]
    doc, _ = read_glb(glb.content)
    assert "Esri" in doc["asset"]["copyright"]
    assert len(glb.content) == j["info"]["glb_bytes"]


def test_texture_off_and_synthetic_have_no_glb(client):
    bbox = {"south": 45.8, "west": 7.4, "north": 45.9, "east": 7.6}
    r = client.post(
        "/api/model",
        json={"source": "terrarium", "resolution": 64, "bbox": bbox, "texture": False},
    )
    assert r.status_code == 200 and r.json()["glb_url"] is None
    r = client.post("/api/model", json={"source": "synthetic", "resolution": 64})
    assert r.status_code == 200 and r.json()["glb_url"] is None
    assert r.json()["info"]["textured"] is False


def test_imagery_failure_still_returns_stl():
    client = make_client(imagery_fetcher=FakeImageryFetcher(fail=True))
    r = client.post(
        "/api/model",
        json={
            "source": "terrarium",
            "resolution": 64,
            # a bbox no other test uses, so the imagery cache cannot answer
            "bbox": {"south": 10.0, "west": 20.0, "north": 10.1, "east": 20.1},
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["glb_url"] is None
    assert "unreachable" in j["info"]["texture_error"]
    assert client.get(j["stl_url"]).status_code == 200


CITY = {"south": 40.70, "west": -74.02, "north": 40.72, "east": -74.00}


def test_buildings_are_added_to_stl_and_glb(client):
    base = {"source": "terrarium", "resolution": 64, "bbox": CITY, "width_mm": 100}
    plain = client.post("/api/model", json=base).json()
    r = client.post("/api/model", json={**base, "buildings": True, "name": "city"})
    assert r.status_code == 200, r.text
    j = r.json()
    info = j["info"]
    assert info["buildings"] == 2 and "OpenStreetMap" in info["building_attribution"]
    assert info["triangles"] > plain["info"]["triangles"]
    # the 300 m building at true proportion sets the model height
    assert info["height_mm"] > plain["info"]["height_mm"]
    tri = read_binary_stl(client.get(j["stl_url"]).content)
    assert tri.shape[0] == info["triangles"]
    assert tri[..., 2].max() == pytest.approx(info["height_mm"], abs=1e-3)
    doc, _ = read_glb(client.get(j["glb_url"]).content)
    assert len(doc["meshes"][0]["primitives"]) == 4
    assert "OpenStreetMap" in doc["asset"]["copyright"]


def test_building_problems_do_not_fail_the_model():
    client = make_client(overpass=FakeOverpass(fail=True))
    r = client.post(
        "/api/model",
        json={
            "source": "terrarium",
            "resolution": 64,
            "bbox": {"south": 41.0, "west": -74.0, "north": 41.01, "east": -73.99},
            "buildings": True,
        },
    )
    assert r.status_code == 200, r.text
    assert "429" in r.json()["info"]["building_error"]

    overpass = FakeOverpass()
    client = make_client(overpass=overpass)
    big = {"south": 45.0, "west": 7.0, "north": 45.2, "east": 7.3}  # ~500 km2
    r = client.post(
        "/api/model",
        json={"source": "terrarium", "resolution": 64, "bbox": big, "buildings": True},
    )
    assert r.status_code == 200
    assert "limited to 40 km2" in r.json()["info"]["building_error"]
    assert overpass.calls == 0


# own areas: the app caches by area, across tests
BRIDGE_AREA = {"south": 40.85, "west": -73.99, "north": 40.868, "east": -73.968}
BRIDGE_ERROR_AREA = {"south": 40.90, "west": -73.99, "north": 40.91, "east": -73.98}


def test_bridges_are_added_to_the_stl():
    vt = FakeVectorTiles()
    client = make_client(vector_tiles=vt)
    base = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": BRIDGE_AREA,
        "texture": False,
    }
    plain = client.post("/api/model", json=base).json()["info"]
    assert "bridges" not in plain
    r = client.post("/api/model", json={**base, "bridges": True})
    assert r.status_code == 200, r.text
    info = r.json()["info"]
    assert info["bridges"] >= 1 and "buildings" not in info
    assert info["triangles"] > plain["triangles"]
    assert info["height_mm"] > plain["height_mm"]
    tri = read_binary_stl(client.get(r.json()["stl_url"]).content)
    assert tri.shape[0] == info["triangles"]
    # features are cached: the same area again does not fetch tiles again
    calls = vt.calls
    again = client.post(
        "/api/model", json={**base, "bridges": True, "bridge_piers_mm": 0}
    )
    assert vt.calls == calls
    assert again.json()["info"]["triangles"] < info["triangles"]  # no piers
    assert again.json()["info"]["bridges"] == info["bridges"]


def test_bridges_work_with_buildings_in_the_glb_and_3mf():
    import zipfile

    client = make_client(vector_tiles=FakeVectorTiles())
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": BRIDGE_AREA,
        "bridges": True,
        "multicolor": True,
    }
    j = client.post("/api/model", json=body).json()
    info = j["info"]
    # bridges share the buildings part, and OSM is credited without buildings
    assert info["bridges"] >= 1 and info["multicolor_parts"] == [
        "land",
        "water",
        "buildings",
    ]
    doc, _ = read_glb(client.get(j["glb_url"]).content)
    assert "OpenStreetMap" in doc["asset"]["copyright"]
    with zipfile.ZipFile(io.BytesIO(client.get(j["threemf_url"]).content)) as z:
        assert "OpenStreetMap" in z.read("3D/3dmodel.model").decode()
    both = client.post("/api/model", json={**body, "buildings": True}).json()["info"]
    assert both["bridges"] == info["bridges"] and both["buildings"] >= 1


def test_bridge_problems_do_not_fail_the_model(client):
    base = {"source": "terrarium", "resolution": 64, "bbox": BRIDGE_ERROR_AREA}
    r = client.post("/api/model", json={**base, "bridges": True})  # tiles fail
    assert r.status_code == 200, r.text
    info = r.json()["info"]
    assert "unreachable" in info["bridge_error"] and "bridges" not in info
    big = {"south": 45.0, "west": 7.0, "north": 45.2, "east": 7.3}  # ~500 km2
    r = client.post("/api/model", json={**base, "bbox": big, "bridges": True})
    assert r.status_code == 200
    assert "limited to 40 km2" in r.json()["info"]["bridge_error"]


def test_search(client):
    r = client.get("/api/search", params={"q": "Zermatt"})
    assert r.status_code == 200
    (hit,) = r.json()
    assert hit["name"] == "Zermatt" and hit["bbox"] == [45.9, 7.5, 46.1, 7.7]
    assert client.get("/api/search", params={"q": "x"}).status_code == 422
    assert client.get("/api/search", params={"q": "boom"}).status_code == 502


def _wait(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_background_job_reports_stages_and_result(client):
    body = {"source": "terrarium", "resolution": 64, "bbox": CITY, "buildings": True}
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 200
    j = _wait(client, r.json()["job_id"])
    assert j["status"] == "done", j
    res = j["result"]
    assert res["info"]["buildings"] == 2
    assert client.get(res["stl_url"]).status_code == 200
    assert client.get("/api/jobs/nope").status_code == 404


def test_background_job_reports_errors(client):
    j = _wait(
        client, client.post("/api/jobs", json={"source": "terrarium"}).json()["job_id"]
    )
    assert j["status"] == "error" and j["status_code"] == 400
    assert "bbox is required" in j["error"]
    assert client.post("/api/jobs", json={"width_mm": 1}).status_code == 422


def test_openfreemap_is_preferred_and_named():
    vt = FakeVectorTiles()
    overpass = FakeOverpass()
    client = make_client(vector_tiles=vt, overpass=overpass)
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.75, "west": -73.99, "north": 40.76, "east": -73.98},
        "buildings": True,
    }
    info = client.post("/api/model", json=body).json()["info"]
    assert info["building_source"] == "openfreemap" and info["buildings"] >= 1
    assert overpass.calls == 0 and overpass.shaped_calls == 1
    assert "building_warning" not in info

    body["bbox"] = {"south": 40.76, "west": -73.99, "north": 40.77, "east": -73.98}
    info = client.post(
        "/api/model", json={**body, "building_source": "overpass"}
    ).json()["info"]
    assert info["building_source"] == "overpass" and overpass.calls == 1


def test_auto_falls_back_to_overpass_and_reports_both_failures():
    client = make_client()  # vector tiles fail by default
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.77, "west": -73.99, "north": 40.78, "east": -73.98},
        "buildings": True,
    }
    info = client.post("/api/model", json=body).json()["info"]
    assert info["building_source"] == "overpass"

    client = make_client(overpass=FakeOverpass(fail=True))
    body["bbox"] = {"south": 40.78, "west": -73.99, "north": 40.79, "east": -73.98}
    err = client.post("/api/model", json=body).json()["info"]["building_error"]
    assert (
        "openfreemap: tiles.openfreemap.org unreachable" in err and "overpass:" in err
    )


def test_multicolor_3mf_has_land_water_and_buildings():
    import zipfile

    client = make_client(vector_tiles=FakeVectorTiles())
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.80, "west": -73.99, "north": 40.81, "east": -73.98},
        "buildings": True,
        "multicolor": True,
    }
    j = client.post("/api/model", json=body).json()
    info = j["info"]
    assert info["multicolor_parts"] == ["land", "water", "buildings"]
    assert 0 < info["water_fraction"] < 1
    r = client.get(j["threemf_url"])
    assert (
        r.headers["content-type"] == "model/3mf"
        and len(r.content) == info["threemf_bytes"]
    )
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        model = z.read("3D/3dmodel.model").decode()
    assert model.count("<component ") == 3

    plain = client.post("/api/model", json={**body, "multicolor": False}).json()
    assert plain["threemf_url"] is None


def test_multicolor_without_water_tiles_still_writes_3mf():
    client = make_client()  # vector tiles fail
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.82, "west": -73.99, "north": 40.83, "east": -73.98},
        "multicolor": True,
    }
    j = client.post("/api/model", json=body).json()
    assert "unreachable" in j["info"]["water_error"] and j["threemf_url"]


def test_frame_goes_into_stl_glb_and_3mf():
    import xml.etree.ElementTree as ET
    import zipfile

    client = make_client(vector_tiles=FakeVectorTiles())
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.84, "west": -73.99, "north": 40.85, "east": -73.98},
        "width_mm": 100,
        "base_mm": 3,
        "buildings": True,
        "multicolor": True,
        "frame_mm": 6,
    }
    j = client.post("/api/model", json=body).json()
    info = j["info"]
    assert info["outer_width_mm"] == pytest.approx(112)
    assert info["frame_height_mm"] == pytest.approx(4)
    tri = read_binary_stl(client.get(j["stl_url"]).content)
    assert tri[..., 0].min() == pytest.approx(-6) and tri[
        ..., 0
    ].max() == pytest.approx(106)
    assert info["multicolor_parts"] == ["land", "water", "buildings", "border"]
    with zipfile.ZipFile(io.BytesIO(client.get(j["threemf_url"]).content)) as z:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    parts = {
        m.get("value")
        for p in cfg.iter("part")
        for m in p.findall("metadata")
        if m.get("key") == "name"
    }
    border = [p for p in cfg.iter("part") if any(m.get("value") == "border" for m in p)]
    assert parts == {"land", "water", "buildings", "border"}
    assert [m.get("value") for m in border[0] if m.get("key") == "extruder"] == ["4"]
    doc, _ = read_glb(client.get(j["glb_url"]).content)
    xs = [a for a in doc["accessors"] if a["type"] == "VEC3" and "min" in a]
    assert min(a["min"][0] for a in xs) == pytest.approx(-0.006, abs=1e-6)


def test_openfreemap_keeps_flat_roofs_when_shapes_fail():
    client = make_client(
        vector_tiles=FakeVectorTiles(), overpass=FakeOverpass(fail=True)
    )
    body = {
        "source": "terrarium",
        "resolution": 64,
        "bbox": {"south": 40.90, "west": -73.99, "north": 40.91, "east": -73.98},
        "buildings": True,
    }
    info = client.post("/api/model", json=body).json()["info"]
    assert info["building_source"] == "openfreemap" and info["buildings"] >= 1
    assert info["building_warning"].startswith("roof shapes unavailable")
