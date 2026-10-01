"""Elevation data sources and heightmap processing.

Sources
-------
* ``fetch_terrarium`` — satellite-derived global elevation (SRTM/ASTER/GMTED
  merged) from the AWS Open Data "Terrain Tiles" set, in Mapzen *terrarium*
  PNG encoding. No API key is required.
* ``synthetic_heightmap`` — procedural mountains for offline use and tests.
* ``load_heightmap_file`` — user-supplied PNG / TIFF / GeoTIFF heightmaps.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import requests
from PIL import Image

TERRARIUM_URL = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
TILE_SIZE = 256
MAX_ZOOM = 14  # terrarium tiles exist to z15; z14 (~10 m/px) is plenty
MAX_TILES = 64  # guard against accidental multi-gigabyte requests
EARTH_RADIUS_M = 6_371_008.8

Image.MAX_IMAGE_PIXELS = 50_000_000


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def __post_init__(self):
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("latitude bounds must satisfy -90 <= south < north <= 90")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("longitude bounds must satisfy -180 <= west < east <= 180")
        if abs(self.south) > 85 or abs(self.north) > 85:
            raise ValueError("areas beyond +/-85 degrees latitude are not covered")

    @property
    def mid_lat(self) -> float:
        return (self.south + self.north) / 2

    @property
    def mid_lon(self) -> float:
        return (self.west + self.east) / 2

    def ground_size_m(self) -> tuple[float, float]:
        """(width, height) in metres along the bbox's central lines."""
        w = haversine_m(self.mid_lat, self.west, self.mid_lat, self.east)
        h = haversine_m(self.south, self.mid_lon, self.north, self.mid_lon)
        return w, h


@dataclass
class Heightmap:
    data: np.ndarray  # (rows, cols) float32 metres; row 0 = north
    ground_width_m: float
    ground_height_m: float
    source: str
    bbox: BBox | None = None
    meta: dict = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape

    @property
    def min(self) -> float:
        return float(np.nanmin(self.data))

    @property
    def max(self) -> float:
        return float(np.nanmax(self.data))


# ----------------------------------------------------------------------------
# Geodesy helpers
# ----------------------------------------------------------------------------


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def lonlat_to_global_px(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates (x right, y down) at ``zoom``."""
    n = TILE_SIZE * (2**zoom)
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def choose_zoom(bbox: BBox, target_cols: int) -> int:
    """Smallest zoom at which the bbox spans at least ``target_cols`` pixels."""
    for z in range(0, MAX_ZOOM + 1):
        x0, _ = lonlat_to_global_px(bbox.west, bbox.north, z)
        x1, _ = lonlat_to_global_px(bbox.east, bbox.south, z)
        if x1 - x0 >= target_cols:
            return z
    return MAX_ZOOM


# ----------------------------------------------------------------------------
# Terrarium tiles
# ----------------------------------------------------------------------------


def decode_terrarium(png_bytes: bytes) -> np.ndarray:
    """Decode a terrarium PNG into float32 metres."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    rgb = np.asarray(img, dtype=np.float32)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def encode_terrarium(elev: np.ndarray) -> bytes:
    """Inverse of :func:`decode_terrarium` (used by tests and fixtures)."""
    v = np.asarray(elev, dtype=np.float64) + 32768.0
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - r * 256.0 - g) * 256.0)
    rgb = np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


class TileFetcher:
    """Fetch + disk-cache terrarium tiles."""

    def __init__(
        self,
        cache_dir: str | None = None,
        session: requests.Session | None = None,
        url_template: str = TERRARIUM_URL,
        timeout: float = 30.0,
    ):
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "satprint", "tiles"
        )
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "satprint/0.1 (+terrain relief models)"
        )
        self.url_template = url_template
        self.timeout = timeout

    def _cache_path(self, z, x, y) -> str:
        return os.path.join(self.cache_dir, str(z), str(x), f"{y}.png")

    def fetch_bytes(self, z: int, x: int, y: int) -> bytes:
        path = self._cache_path(z, x, y)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
        url = self.url_template.format(z=z, x=x, y=y)
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            raise LookupError(f"tile {z}/{x}/{y} not found at source")
        resp.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp, path)
        return resp.content

    def fetch(self, z: int, x: int, y: int) -> np.ndarray:
        return decode_terrarium(self.fetch_bytes(z, x, y))


def fetch_terrarium(
    bbox: BBox,
    target_cols: int = 256,
    fetcher: TileFetcher | None = None,
    workers: int = 8,
) -> Heightmap:
    """Download, mosaic and crop terrain tiles covering ``bbox``.

    The result is resampled so that its pixel aspect ratio matches the
    ground aspect ratio (width/height in metres), with ``target_cols`` columns.
    """
    fetcher = fetcher or TileFetcher()
    zoom = choose_zoom(bbox, target_cols)
    x0, y0 = lonlat_to_global_px(bbox.west, bbox.north, zoom)
    x1, y1 = lonlat_to_global_px(bbox.east, bbox.south, zoom)
    tx0, ty0 = int(x0 // TILE_SIZE), int(y0 // TILE_SIZE)
    tx1, ty1 = int(math.ceil(x1 / TILE_SIZE)) - 1, int(math.ceil(y1 / TILE_SIZE)) - 1
    n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if n_tiles > MAX_TILES:
        raise ValueError(
            f"area needs {n_tiles} tiles at zoom {zoom}; lower the resolution or shrink the area"
        )

    coords = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    with ThreadPoolExecutor(max_workers=min(workers, len(coords))) as pool:
        tiles = list(pool.map(lambda c: fetcher.fetch(zoom, c[0], c[1]), coords))

    mosaic = np.empty(
        ((ty1 - ty0 + 1) * TILE_SIZE, (tx1 - tx0 + 1) * TILE_SIZE), dtype=np.float32
    )
    for (tx, ty), tile in zip(coords, tiles):
        r, c = (ty - ty0) * TILE_SIZE, (tx - tx0) * TILE_SIZE
        mosaic[r : r + TILE_SIZE, c : c + TILE_SIZE] = tile

    # Crop to the exact bbox in pixel space.
    cx0, cy0 = x0 - tx0 * TILE_SIZE, y0 - ty0 * TILE_SIZE
    cx1, cy1 = x1 - tx0 * TILE_SIZE, y1 - ty0 * TILE_SIZE
    crop = mosaic[int(cy0) : int(math.ceil(cy1)), int(cx0) : int(math.ceil(cx1))]

    gw, gh = bbox.ground_size_m()
    rows = max(2, int(round(target_cols * gh / gw)))
    data = resample(crop, rows, target_cols)
    return Heightmap(
        data=data,
        ground_width_m=gw,
        ground_height_m=gh,
        source="terrarium",
        bbox=bbox,
        meta={"zoom": zoom, "tiles": n_tiles, "native_px": list(crop.shape)},
    )


# ----------------------------------------------------------------------------
# Synthetic terrain (offline demo / tests)
# ----------------------------------------------------------------------------


def synthetic_heightmap(
    rows: int = 200, cols: int = 256, seed: int = 0, ground_width_m: float = 20_000.0
) -> Heightmap:
    """Procedural alpine-looking terrain with ridges, valleys and a lake."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:rows, 0:cols].astype(np.float64)
    x /= cols
    y /= rows

    elev = np.zeros((rows, cols))
    # Fractal value noise: sum of upsampled random grids.
    amp, cells = 1.0, 4
    for _ in range(6):
        grid = rng.random((cells + 1, cells + 1)).astype(np.float32)
        layer = np.asarray(
            Image.fromarray(grid, "F").resize((cols, rows), Image.Resampling.BICUBIC)
        )
        elev += amp * (layer - 0.5)
        amp *= 0.5
        cells *= 2
    # A couple of big peaks.
    for _ in range(3):
        px, py, s = rng.random(), rng.random(), 0.12 + 0.1 * rng.random()
        elev += 1.2 * np.exp(-((x - px) ** 2 + (y - py) ** 2) / (2 * s * s))
    # Ridged transform for sharper crests.
    elev = 1.0 - np.abs(elev - elev.mean())
    elev = (elev - elev.min()) / (elev.max() - elev.min())
    elev = 400.0 + 2400.0 * elev**1.6
    # Flat lake in the lowest basin.
    lake = np.percentile(elev, 8)
    elev = np.where(elev < lake, lake, elev)
    gh = ground_width_m * rows / cols
    return Heightmap(
        data=elev.astype(np.float32),
        ground_width_m=ground_width_m,
        ground_height_m=gh,
        source="synthetic",
        meta={"seed": seed},
    )


# ----------------------------------------------------------------------------
# File import
# ----------------------------------------------------------------------------


def load_heightmap_file(
    data: bytes,
    filename: str = "",
    ground_width_m: float | None = None,
    max_cols: int = 1024,
) -> Heightmap:
    """Load a heightmap image. GeoTIFFs use rasterio when available, otherwise
    the file is treated as a plain raster whose values are metres (16-bit PNGs
    are interpreted as metres as well, 8-bit as 0-255 relative units)."""
    arr: np.ndarray | None = None
    meta: dict = {"filename": filename}
    gw = ground_width_m
    gh = None

    if filename.lower().endswith((".tif", ".tiff")):
        try:
            from rasterio.io import MemoryFile  # ty: ignore[unresolved-import]  # optional dependency

            with MemoryFile(data) as mem, mem.open() as ds:
                arr = ds.read(1).astype(np.float32)
                if ds.nodata is not None:
                    arr[arr == ds.nodata] = np.nan
                if ds.crs and ds.bounds:
                    b = ds.bounds
                    if ds.crs.is_geographic:
                        bbox = BBox(b.bottom, b.left, b.top, b.right)
                        gw, gh = bbox.ground_size_m()
                        meta["bbox"] = bbox.__dict__
                    else:
                        gw, gh = float(b.right - b.left), float(b.top - b.bottom)
                meta["georeferenced"] = gw is not None
        except ImportError:
            arr = None

    if arr is None:
        img = Image.open(io.BytesIO(data))
        if img.mode in ("I;16", "I;16B", "I", "F"):
            arr = np.asarray(img.convert("F"), dtype=np.float32)
        else:
            arr = np.asarray(img.convert("L"), dtype=np.float32)

    if arr.ndim != 2:
        raise ValueError("heightmap must be single-band")
    # Fill nodata holes with the minimum valid elevation.
    if np.isnan(arr).any():
        arr = np.where(np.isnan(arr), np.nanmin(arr), arr)
    rows, cols = arr.shape
    if cols > max_cols:
        rows = max(2, int(round(rows * max_cols / cols)))
        arr = resample(arr, rows, max_cols)
        cols = max_cols
    if gw is None:
        gw = 30.0 * cols  # assume ~30 m pixels (SRTM-like) if nothing better
        meta["assumed_pixel_m"] = 30.0
    if gh is None:
        gh = gw * rows / cols
    return Heightmap(
        data=arr.astype(np.float32),
        ground_width_m=float(gw),
        ground_height_m=float(gh),
        source="upload",
        meta=meta,
    )


# ----------------------------------------------------------------------------
# Processing
# ----------------------------------------------------------------------------


def resample(arr: np.ndarray, rows: int, cols: int) -> np.ndarray:
    img = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32), "F")
    method = (
        Image.Resampling.BOX
        if (cols < arr.shape[1] or rows < arr.shape[0])
        else Image.Resampling.BICUBIC
    )
    return np.asarray(img.resize((cols, rows), method), dtype=np.float32)


def gaussian_smooth(arr: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur with edge replication (no SciPy needed)."""
    if sigma <= 0:
        return arr
    radius = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(arr.astype(np.float64), radius, mode="edge")
    tmp = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 1, padded)
    out = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 0, tmp)
    return out.astype(np.float32)


@dataclass(frozen=True)
class PrintParams:
    width_mm: float = 100.0
    base_mm: float = 3.0
    exaggeration: float = 1.5
    relief_mm: float | None = None  # if set, overrides exaggeration
    smoothing: float = 0.0  # Gaussian sigma in heightmap pixels
    clamp_sea_level: bool = True
    min_feature_mm: float = 0.0  # reserved for future nozzle-aware filtering

    def __post_init__(self):
        if not (10 <= self.width_mm <= 1000):
            raise ValueError("width_mm must be between 10 and 1000")
        if not (0.5 <= self.base_mm <= 100):
            raise ValueError("base_mm must be between 0.5 and 100")
        if not (0.1 <= self.exaggeration <= 20):
            raise ValueError("exaggeration must be between 0.1 and 20")
        if self.relief_mm is not None and not (0.2 <= self.relief_mm <= 500):
            raise ValueError("relief_mm must be between 0.2 and 500")
        if not (0 <= self.smoothing <= 20):
            raise ValueError("smoothing must be between 0 and 20")


def prepare_relief(hm: Heightmap, p: PrintParams) -> tuple[np.ndarray, dict]:
    """Convert elevation (m) to relief heights (mm above the base) and report
    the scale actually used."""
    elev = hm.data.astype(np.float32)
    if p.clamp_sea_level:
        elev = np.maximum(elev, 0.0)
    if p.smoothing > 0:
        elev = gaussian_smooth(elev, p.smoothing)
    lo, hi = float(np.min(elev)), float(np.max(elev))
    span = max(hi - lo, 1e-6)

    mm_per_m_plan = p.width_mm / hm.ground_width_m  # horizontal scale
    if p.relief_mm is not None:
        z_scale = p.relief_mm / span
        exaggeration = z_scale / mm_per_m_plan
    else:
        z_scale = mm_per_m_plan * p.exaggeration
        exaggeration = p.exaggeration
    relief = (elev - lo) * z_scale
    depth_mm = p.width_mm * hm.ground_height_m / hm.ground_width_m
    info = {
        "min_elev_m": lo,
        "max_elev_m": hi,
        "relief_m": span,
        "width_mm": p.width_mm,
        "depth_mm": depth_mm,
        "base_mm": p.base_mm,
        "relief_mm": float(np.max(relief)),
        "height_mm": float(np.max(relief)) + p.base_mm,
        "plan_scale": f"1:{int(round(1 / mm_per_m_plan * 1000)):,}",
        "mm_per_m_plan": mm_per_m_plan,
        "mm_per_m_vertical": z_scale,
        "exaggeration": exaggeration,
        "rows": int(elev.shape[0]),
        "cols": int(elev.shape[1]),
    }
    return relief.astype(np.float32), info


def heightmap_png(hm: Heightmap, clamp_sea_level: bool = True) -> bytes:
    """Hillshaded 8-bit preview PNG of the heightmap."""
    elev = hm.data.astype(np.float64)
    if clamp_sea_level:
        elev = np.maximum(elev, 0)
    lo, hi = np.min(elev), np.max(elev)
    norm = (elev - lo) / max(hi - lo, 1e-6)
    gy, gx = np.gradient(elev)
    # light from the north-west, 45 deg elevation
    scale = 2.0 * (elev.shape[1] / hm.ground_width_m)
    nx, ny, nz = -gx * scale, gy * scale, np.ones_like(elev)
    n = np.sqrt(nx * nx + ny * ny + nz * nz)
    light = np.array([-0.5, 0.5, 0.7071])
    shade = np.clip((nx * light[0] + ny * light[1] + nz * light[2]) / n, 0, 1)
    img = np.clip(255 * (0.25 + 0.45 * norm + 0.3 * shade), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img, "L").save(buf, format="PNG")
    return buf.getvalue()


def heightmap_digest(hm: Heightmap) -> str:
    h = hashlib.sha1()
    h.update(hm.data.tobytes())
    h.update(f"{hm.ground_width_m:.3f}:{hm.ground_height_m:.3f}".encode())
    return h.hexdigest()[:16]
