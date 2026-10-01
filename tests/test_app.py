import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from satprint.app import create_app
from satprint.mesh import read_binary_stl
from tests.test_terrain import FakeFetcher


@pytest.fixture
def client():
    return TestClient(create_app(tile_fetcher=FakeFetcher()))


def test_index_and_presets(client):
    assert "satprint" in client.get("/").text
    presets = client.get("/api/presets").json()
    assert presets and all(len(p["bbox"]) == 4 for p in presets)
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
