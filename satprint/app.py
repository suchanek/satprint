"""FastAPI backend: terrain lookup, model generation, STL and GLB download."""

from __future__ import annotations

import base64
import io
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .buildings import (
    MAX_BUILDING_AREA_KM2,
    BuildingMesh,
    bbox_area_km2,
    building_mesh,
)
from .mesh import (
    Mesh,
    frame_mesh,
    heightmap_to_mesh,
    merge_meshes,
    write_3mf,
    write_binary_stl,
    write_glb,
)
from .osm import (
    OSM_ATTRIBUTION,
    OVERTURE_ATTRIBUTION,
    Geocoder,
    OverpassClient,
    VectorTileClient,
    fetch_buildings,
)
from .presets import presets as all_presets
from .terrain import (
    IMAGERY_ATTRIBUTION,
    BBox,
    Heightmap,
    ImageryFetcher,
    PrintParams,
    Progress,
    TileFetcher,
    fetch_imagery,
    fetch_terrarium,
    heightmap_png,
    load_heightmap_file,
    prepare_relief,
    synthetic_heightmap,
)
from .water import multicolor_parts, water_from_vector_tiles, water_zoom

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

PRESETS = all_presets()

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
    glb: bytes | None = None
    threemf: bytes | None = None


@dataclass
class Job:
    """A background model build and how far it has got."""

    id: str
    status: str = "running"  # running | done | error
    stage: str = "starting"
    done: int = 0
    total: int = 0  # 0 while a stage has no count
    started: float = field(default_factory=time.time)
    result: dict | None = None
    error: str | None = None
    status_code: int | None = None

    def as_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(time.time() - self.started, 1),
            "result": self.result,
            "error": self.error,
            "status_code": self.status_code,
        }


jobs = _LRU(64)  # job_id -> Job
terrain_cache = _LRU(32)  # key -> Heightmap   (downloads are the slow part)
imagery_cache = _LRU(16)  # bbox key -> (JPEG bytes, meta)
water_cache = _LRU(16)  # bbox key -> list of water polygons
buildings_cache = _LRU(16)  # bbox key -> list[Building]
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
    bbox: BBoxIn | None = None
    upload_id: str | None = None
    ground_width_m: float | None = Field(None, gt=0, description="override for uploads")
    seed: int = 0
    resolution: int = Field(256, ge=32, le=1024, description="grid columns")
    width_mm: float = Field(100.0, ge=10, le=1000)
    base_mm: float = Field(3.0, ge=0.5, le=100)
    exaggeration: float = Field(1.5, ge=0.1, le=20)
    relief_mm: float | None = Field(None, ge=0.2, le=500)
    smoothing: float = Field(0.0, ge=0, le=20)
    clamp_sea_level: bool = True
    texture: bool = Field(
        True, description="drape satellite imagery on a GLB (terrarium source only)"
    )
    buildings: bool = Field(
        False, description="add OpenStreetMap buildings (terrarium source only)"
    )
    building_scale: float = Field(
        1.0,
        ge=0.25,
        le=20,
        description="building height multiplier; 1 = true proportion",
    )
    building_source: Literal["auto", "openfreemap", "overpass", "overture"] = Field(
        "auto",
        description="OpenFreeMap vector tiles (fast, rebuilt weekly), Overpass "
        "(latest OSM edits, slower), Overture Maps (OSM plus Microsoft and "
        "Google footprints; needs the overture extra), or auto: OpenFreeMap, then Overpass",
    )
    frame_mm: float = Field(
        0.0, ge=0, le=30, description="border frame width around the model; 0 = none"
    )
    frame_height_mm: float | None = Field(
        None, gt=0, le=100, description="frame height; default base thickness + 1 mm"
    )
    multicolor: bool = Field(
        False,
        description="also write a 3MF with land, water and buildings as separate "
        "parts, for multi-material printers (terrarium source only)",
    )
    name: str = Field("terrain", max_length=60)


class ModelResponse(BaseModel):
    model_id: str
    stl_url: str
    png_url: str
    glb_url: str | None = None
    threemf_url: str | None = None
    info: dict
    preview_png: str  # data URL


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------


def create_app(
    tile_fetcher: TileFetcher | None = None,
    imagery_fetcher: ImageryFetcher | None = None,
    overpass: OverpassClient | None = None,
    geocoder: Geocoder | None = None,
    vector_tiles: VectorTileClient | None = None,
) -> FastAPI:
    app = FastAPI(
        title="satprint",
        version=__version__,
        description="Turn satellite elevation data into 3D-printable terrain models.",
    )
    fetcher = tile_fetcher or TileFetcher()
    imagery = imagery_fetcher or ImageryFetcher()
    osm = overpass or OverpassClient()
    vtiles = vector_tiles or VectorTileClient()

    def _water_for(bbox: BBox, progress: Progress | None = None) -> list:
        key = _bbox_key(bbox)
        hit = water_cache.get(key)
        if hit is None:
            tiles = vtiles.tiles(
                bbox, progress=progress, zoom=water_zoom(bbox), stage="water"
            )
            hit = water_from_vector_tiles(tiles)
            water_cache.put(key, hit)
        return hit

    geo = geocoder or Geocoder()

    def _bbox_key(bbox: BBox) -> tuple:
        return tuple(
            round(v, 5) for v in (bbox.south, bbox.west, bbox.north, bbox.east)
        )

    def _buildings_for(
        bbox: BBox, source: str, progress: Progress | None = None
    ) -> tuple[list, str | None, str]:
        """Buildings in ``bbox``, a roof-shape warning, and the source used."""
        order = {"auto": ["openfreemap", "overpass"]}.get(source, [source])
        errors = []
        for name in order:
            key = (name, *_bbox_key(bbox))
            hit = buildings_cache.get(key)
            if hit is not None:
                return *hit, name
            try:
                hit = fetch_buildings(name, bbox, osm, vtiles, progress)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            buildings_cache.put(key, hit)
            return *hit, name
        raise RuntimeError("; ".join(errors))

    def _texture_for(
        bbox: BBox, progress: Progress | None = None
    ) -> tuple[bytes, dict]:
        key = _bbox_key(bbox)
        hit = imagery_cache.get(key)
        if hit is None:
            img, meta = fetch_imagery(bbox, fetcher=imagery, progress=progress)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            hit = (buf.getvalue(), meta)
            imagery_cache.put(key, hit)
        return hit

    def _terrain_for(req: ModelRequest, progress: Progress | None = None) -> Heightmap:
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
                hm = fetch_terrarium(
                    bbox, target_cols=req.resolution, fetcher=fetcher, progress=progress
                )
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

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=2, max_length=200)):
        try:
            return geo.search(q)
        except Exception as exc:
            raise HTTPException(502, f"place search failed: {exc}") from exc

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

    def _build(req: ModelRequest, progress: Progress | None = None) -> ModelResponse:
        """Build a model; ``progress`` hears each stage as it advances."""
        hm = _terrain_for(req, progress)
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
        if progress:
            progress("terrain mesh", 0, 0)
        relief, info = prepare_relief(hm, params)
        mesh: Mesh = heightmap_to_mesh(
            relief, info["width_mm"], info["depth_mm"], params.base_mm
        )
        frame: Mesh | None = None
        body = mesh  # terrain plus frame: what the STL and GLB carry
        if req.frame_mm > 0:
            frame_h = req.frame_height_mm or params.base_mm + 1.0
            frame = frame_mesh(
                info["width_mm"], info["depth_mm"], req.frame_mm, frame_h
            )
            body = merge_meshes(mesh, frame)
            info.update(
                {
                    "frame_mm": req.frame_mm,
                    "frame_height_mm": frame_h,
                    "outer_width_mm": info["width_mm"] + 2 * req.frame_mm,
                    "outer_depth_mm": info["depth_mm"] + 2 * req.frame_mm,
                    "height_mm": max(info["height_mm"], frame_h),
                }
            )
        rows, cols = relief.shape
        bmesh: BuildingMesh | None = None
        building_info: dict = {}
        if req.buildings and hm.bbox is not None:
            area = bbox_area_km2(hm.bbox)
            # Like the texture, a building failure should not cost the STL.
            if area > MAX_BUILDING_AREA_KM2:
                building_info["building_error"] = (
                    f"area is {area:.0f} km2; buildings are limited to "
                    f"{MAX_BUILDING_AREA_KM2:.0f} km2"
                )
            else:
                try:
                    found, warning, used = _buildings_for(
                        hm.bbox, req.building_source, progress
                    )
                except Exception as exc:
                    building_info["building_error"] = f"building download failed: {exc}"
                else:
                    bmesh = building_mesh(
                        found,
                        hm.bbox,
                        relief,
                        info["width_mm"],
                        info["depth_mm"],
                        params.base_mm,
                        info["mm_per_m_plan"],
                        scale=req.building_scale,
                        progress=progress,
                    )
                    building_info = {
                        "buildings": bmesh.count,
                        "building_source": used,
                        "building_attribution": (
                            OVERTURE_ATTRIBUTION
                            if used == "overture"
                            else OSM_ATTRIBUTION
                        ),
                    }
                    if warning:
                        building_info["building_warning"] = warning
                    if bmesh.count:
                        top = float(bmesh.vertices[:, 2].max())
                        info["height_mm"] = max(info["height_mm"], top)
        if progress:
            progress("writing files", 0, 0)
        solid = merge_meshes(body, bmesh.as_mesh()) if bmesh and bmesh.count else body
        stl = write_binary_stl(solid, name=req.name)
        png = heightmap_png(hm, params.clamp_sea_level)
        threemf = None
        multicolor_info: dict = {}
        if req.multicolor and hm.bbox is not None:
            polygons: list = []
            try:
                polygons = _water_for(hm.bbox, progress)
            except Exception as exc:
                # Coasts still color from the flattened sea; say why rivers
                # and lakes are missing.
                multicolor_info["water_error"] = f"water download failed: {exc}"
            parts, mask = multicolor_parts(
                polygons,
                hm.bbox,
                relief,
                info["width_mm"],
                info["depth_mm"],
                params.base_mm,
                sea_level_flat=params.clamp_sea_level and info["min_elev_m"] <= 0,
                buildings=bmesh,
                frame=frame,
            )
            sources = [OSM_ATTRIBUTION] if polygons else []
            if bmesh and bmesh.count:
                sources.append(building_info["building_attribution"])
            credits = "; ".join(dict.fromkeys(sources)) or None
            threemf = write_3mf(parts, name=req.name, attribution=credits)
            multicolor_info.update(
                {
                    "multicolor_parts": [p[0] for p in parts if p[1].faces.shape[0]],
                    "water_fraction": round(float(mask.mean()), 4),
                    "threemf_bytes": len(threemf),
                }
            )
        glb = None
        texture_info: dict = {"textured": False}
        if req.texture and hm.bbox is not None:
            # A missing texture should not cost the user their STL.
            try:
                jpeg, meta = _texture_for(hm.bbox, progress)
            except Exception as exc:
                texture_info["texture_error"] = f"imagery download failed: {exc}"
            else:
                credits = IMAGERY_ATTRIBUTION
                if bmesh and bmesh.count:
                    credits += "; " + building_info["building_attribution"]
                glb = write_glb(
                    body,
                    rows,
                    cols,
                    jpeg,
                    name=req.name,
                    copyright=credits,
                    buildings=bmesh,
                )
                texture_info = {
                    "textured": True,
                    "texture_meta": meta,
                    "glb_bytes": len(glb),
                    "texture_attribution": IMAGERY_ATTRIBUTION,
                }
        vol_cm3 = solid.volume_mm3() / 1000.0
        info.update(
            {
                "triangles": solid.triangle_count,
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
                **texture_info,
                **building_info,
                **multicolor_info,
            }
        )
        model_id = uuid.uuid4().hex[:12]
        model_store.put(
            model_id,
            StoredModel(
                stl=stl,
                png=png,
                info=info,
                created=time.time(),
                glb=glb,
                threemf=threemf,
            ),
        )
        safe = (
            "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in req.name)
            or "terrain"
        )
        return ModelResponse(
            model_id=model_id,
            stl_url=f"/api/model/{model_id}/{safe}.stl",
            png_url=f"/api/model/{model_id}/heightmap.png",
            glb_url=f"/api/model/{model_id}/{safe}.glb" if glb else None,
            threemf_url=f"/api/model/{model_id}/{safe}.3mf" if threemf else None,
            info=info,
            preview_png="data:image/png;base64," + base64.b64encode(png).decode(),
        )

    @app.post("/api/model", response_model=ModelResponse)
    def make_model(req: ModelRequest):
        return _build(req)

    @app.post("/api/jobs")
    def start_job(req: ModelRequest):
        """Build a model in the background; poll ``GET /api/jobs/{id}``."""
        job = Job(id=uuid.uuid4().hex[:12])
        jobs.put(job.id, job)

        def report(stage: str, done: int, total: int) -> None:
            job.stage, job.done, job.total = stage, done, total

        def run() -> None:
            try:
                job.result = _build(req, report).model_dump()
                job.status = "done"
            except HTTPException as exc:
                job.error, job.status_code = str(exc.detail), exc.status_code
                job.status = "error"
            except Exception as exc:
                job.error, job.status_code = f"model build failed: {exc}", 500
                job.status = "error"

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found (server restarted?)")
        return job.as_dict()

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
        if filename.endswith(".glb") and m.glb is not None:
            return Response(
                m.glb,
                media_type="model/gltf-binary",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        if filename.endswith(".3mf") and m.threemf is not None:
            return Response(
                m.threemf,
                media_type="model/3mf",
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
