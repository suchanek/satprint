"""FastAPI backend: terrain lookup, model generation, STL download."""

from __future__ import annotations

import base64
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .mesh import Mesh, heightmap_to_mesh, write_binary_stl
from .terrain import (
    BBox,
    Heightmap,
    PrintParams,
    TileFetcher,
    fetch_terrarium,
    heightmap_png,
    load_heightmap_file,
    prepare_relief,
    synthetic_heightmap,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

PRESETS = [
    {"name": "Matterhorn, Switzerland", "bbox": [45.93, 7.58, 46.02, 7.72]},
    {"name": "Grand Canyon, USA", "bbox": [36.02, -112.25, 36.20, -111.95]},
    {"name": "Mount Fuji, Japan", "bbox": [35.28, 138.65, 35.45, 138.82]},
    {"name": "Yosemite Valley, USA", "bbox": [37.68, -119.70, 37.78, -119.50]},
    {"name": "Mount Rainier, USA", "bbox": [46.78, -121.88, 46.93, -121.63]},
    {"name": "Santorini, Greece", "bbox": [36.33, 25.33, 36.48, 25.50]},
    {"name": "Lake Bled, Slovenia", "bbox": [46.33, 14.05, 46.40, 14.15]},
]

# ----------------------------------------------------------------------------
# In-memory stores with a small LRU cap
# ----------------------------------------------------------------------------


class _LRU:
    def __init__(self, cap: int):
        self.cap, self._d, self._lock = cap, OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.cap:
                self._d.popitem(last=False)


@dataclass
class StoredModel:
    stl: bytes
    png: bytes
    info: dict
    created: float


terrain_cache = _LRU(32)  # key -> Heightmap   (downloads are the slow part)
upload_store = _LRU(16)  # upload_id -> Heightmap
model_store = _LRU(32)  # model_id -> StoredModel


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------


class BBoxIn(BaseModel):
    south: float
    west: float
    north: float
    east: float


class ModelRequest(BaseModel):
    source: Literal["terrarium", "synthetic", "upload"] = "terrarium"
    bbox: Optional[BBoxIn] = None
    upload_id: Optional[str] = None
    ground_width_m: Optional[float] = Field(
        None, gt=0, description="override for uploads"
    )
    seed: int = 0
    resolution: int = Field(256, ge=32, le=1024, description="grid columns")
    width_mm: float = Field(100.0, ge=10, le=1000)
    base_mm: float = Field(3.0, ge=0.5, le=100)
    exaggeration: float = Field(1.5, ge=0.1, le=20)
    relief_mm: Optional[float] = Field(None, ge=0.2, le=500)
    smoothing: float = Field(0.0, ge=0, le=20)
    clamp_sea_level: bool = True
    name: str = Field("terrain", max_length=60)


class ModelResponse(BaseModel):
    model_id: str
    stl_url: str
    png_url: str
    info: dict
    preview_png: str  # data URL


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------


def create_app(tile_fetcher: TileFetcher | None = None) -> FastAPI:
    app = FastAPI(
        title="satprint",
        version=__version__,
        description="Turn satellite elevation data into 3D-printable terrain models.",
    )
    fetcher = tile_fetcher or TileFetcher()

    def _terrain_for(req: ModelRequest) -> Heightmap:
        if req.source == "synthetic":
            key = ("synthetic", req.seed, req.resolution)
            hm = terrain_cache.get(key)
            if hm is None:
                hm = synthetic_heightmap(
                    rows=int(req.resolution * 0.75), cols=req.resolution, seed=req.seed
                )
                terrain_cache.put(key, hm)
            return hm
        if req.source == "upload":
            if not req.upload_id:
                raise HTTPException(400, "upload_id is required for source=upload")
            hm = upload_store.get(req.upload_id)
            if hm is None:
                raise HTTPException(
                    404, "upload not found (server restarted?) — upload the file again"
                )
            if req.ground_width_m:
                hm = Heightmap(
                    hm.data,
                    req.ground_width_m,
                    req.ground_width_m * hm.data.shape[0] / hm.data.shape[1],
                    hm.source,
                    hm.bbox,
                    hm.meta,
                )
            return hm
        if req.bbox is None:
            raise HTTPException(400, "bbox is required for source=terrarium")
        try:
            bbox = BBox(**req.bbox.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        key = (
            "terrarium",
            round(bbox.south, 5),
            round(bbox.west, 5),
            round(bbox.north, 5),
            round(bbox.east, 5),
            req.resolution,
        )
        hm = terrain_cache.get(key)
        if hm is None:
            try:
                hm = fetch_terrarium(bbox, target_cols=req.resolution, fetcher=fetcher)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except Exception as exc:  # network / decode failures
                raise HTTPException(502, f"elevation download failed: {exc}") from exc
            terrain_cache.put(key, hm)
        return hm

    @app.get("/api/presets")
    def presets():
        return PRESETS

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > 60 * 1024 * 1024:
            raise HTTPException(413, "file too large (60 MB limit)")
        try:
            hm = load_heightmap_file(data, file.filename or "")
        except Exception as exc:
            raise HTTPException(400, f"could not read heightmap: {exc}") from exc
        upload_id = uuid.uuid4().hex[:12]
        upload_store.put(upload_id, hm)
        return {
            "upload_id": upload_id,
            "rows": hm.shape[0],
            "cols": hm.shape[1],
            "min_elev_m": hm.min,
            "max_elev_m": hm.max,
            "ground_width_m": hm.ground_width_m,
            "ground_height_m": hm.ground_height_m,
            "meta": hm.meta,
        }

    @app.post("/api/model", response_model=ModelResponse)
    def make_model(req: ModelRequest):
        hm = _terrain_for(req)
        try:
            params = PrintParams(
                width_mm=req.width_mm,
                base_mm=req.base_mm,
                exaggeration=req.exaggeration,
                relief_mm=req.relief_mm,
                smoothing=req.smoothing,
                clamp_sea_level=req.clamp_sea_level,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        t0 = time.perf_counter()
        relief, info = prepare_relief(hm, params)
        mesh: Mesh = heightmap_to_mesh(
            relief, info["width_mm"], info["depth_mm"], params.base_mm
        )
        stl = write_binary_stl(mesh, name=req.name)
        png = heightmap_png(hm, params.clamp_sea_level)
        vol_cm3 = mesh.volume_mm3() / 1000.0
        info.update(
            {
                "triangles": mesh.triangle_count,
                "volume_cm3": vol_cm3,
                "est_weight_g_pla_solid": vol_cm3 * 1.24,
                "est_weight_g_pla_20pct": vol_cm3
                * 1.24
                * 0.35,  # shell + 20 % infill rule of thumb
                "stl_bytes": len(stl),
                "source": hm.source,
                "source_meta": hm.meta,
                "ground_width_m": hm.ground_width_m,
                "ground_height_m": hm.ground_height_m,
                "generate_seconds": round(time.perf_counter() - t0, 3),
            }
        )
        model_id = uuid.uuid4().hex[:12]
        model_store.put(
            model_id, StoredModel(stl=stl, png=png, info=info, created=time.time())
        )
        safe = (
            "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in req.name)
            or "terrain"
        )
        return ModelResponse(
            model_id=model_id,
            stl_url=f"/api/model/{model_id}/{safe}.stl",
            png_url=f"/api/model/{model_id}/heightmap.png",
            info=info,
            preview_png="data:image/png;base64," + base64.b64encode(png).decode(),
        )

    @app.get("/api/model/{model_id}/{filename}")
    def download(model_id: str, filename: str):
        m = model_store.get(model_id)
        if m is None:
            raise HTTPException(404, "model not found — generate it again")
        if filename.endswith(".png"):
            return Response(m.png, media_type="image/png")
        if filename.endswith(".stl"):
            return Response(
                m.stl,
                media_type="model/stl",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        if filename == "info.json":
            return m.info
        raise HTTPException(404, "unknown file")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
