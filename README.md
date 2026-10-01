# satprint — satellite terrain → 3D print

Pick an area on a satellite map, get a watertight, print-ready STL of the
terrain. Real elevation data, scaled to your printer, with a solid base.

```
┌─────────────────────────┐   ┌────────────────────────────┐   ┌──────────────┐
│ Satellite elevation     │ → │ heightmap (metres, grid)   │ → │ STL solid    │
│ AWS Terrain Tiles       │   │ clamp sea, smooth, scale   │   │ top + walls  │
│ (SRTM / ASTER / GMTED)  │   │ to mm, exaggerate relief   │   │ + flat base  │
└─────────────────────────┘   └────────────────────────────┘   └──────────────┘
```

![satprint web UI](docs/screenshot.png)

Mount Fuji from real elevation tiles, 120 mm wide, 1.2× exaggeration:

![Mount Fuji preview](docs/fuji-preview.png)

## Features

* **Web front end** — Leaflet map with Esri satellite imagery, drag-a-rectangle
  area selection, presets (Matterhorn, Grand Canyon, Fuji, …), live three.js
  preview of the generated STL, hillshade thumbnail, print stats and download.
* **Real data, no API key** — elevation from the public
  [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) set
  (terrarium encoding), tiles cached on disk under `~/.cache/satprint/tiles`.
* **Your own data** — upload a 16-bit PNG, TIFF or GeoTIFF heightmap
  (GeoTIFF georeferencing is read when `rasterio` is installed).
* **Offline demo** — procedural alpine terrain so the app and tests run with
  no network.
* **Print-aware output** — true plan scale, vertical exaggeration *or* a fixed
  relief height, base thickness, sea-level flattening, Gaussian smoothing,
  grid resolution up to 1024 columns. Reports model size, scale, triangle
  count, volume and an estimated PLA weight.
* **Guaranteed watertight** — every edge shared by exactly two outward-wound
  triangles; the bottom is a fan so there are no T-junctions. Slicers
  (PrusaSlicer, Cura, Bambu Studio, Chitubox) load it without repair.
* **CLI + REST API** for batch work; interactive API docs at `/docs`.

## Install

```bash
cd satprint
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ",geotiff" for GeoTIFF support (rasterio)
```

## Run the web app

```bash
satprint serve                   # http://127.0.0.1:8000
# or: python -m satprint serve --host 0.0.0.0 --port 8000
```

1. **Choose an area** — click *Draw rectangle* and drag on the map, pick a
   preset, or type the bounds. The hint line shows the real size of the area
   and the resulting model footprint.
2. **Print settings** — model width (mm), base thickness, vertical
   exaggeration (1.5–3× reads well for most landscapes; 1× is true scale), or
   a fixed relief height. Resolution sets the grid columns (256 is fine for
   FDM, 512–1024 for resin). Smoothing hides sensor noise on flat areas.
3. **Generate** — the STL appears in the 3D viewer; download it and slice.

## Command line

```bash
# Matterhorn, 120 mm wide, 2x exaggeration
satprint build --bbox 45.93 7.58 46.02 7.72 --width 120 --exaggeration 2 -o matterhorn.stl

# Fixed 15 mm relief, smoothed, with a hillshade preview PNG
satprint build --bbox 36.02 -112.25 36.20 -111.95 --relief 15 --smoothing 1 \
               --preview canyon.png -o grand-canyon.stl

# From your own DEM (metres), 12 km across in the real world
satprint build --file dem.tif --ground-width 12000 -o dem.stl

# Offline demo terrain
satprint build --synthetic -o demo.stl
```

Bounds are `SOUTH WEST NORTH EAST` in decimal degrees. The command prints a
JSON summary (elevation range, scale, triangle count, volume, watertight check).

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/presets` | Named example areas |
| `POST` | `/api/upload` | Multipart heightmap upload → `upload_id` |
| `POST` | `/api/model` | Build a model; returns stats, preview PNG and download URLs |
| `GET` | `/api/model/{id}/{name}.stl` | Binary STL |
| `GET` | `/api/model/{id}/heightmap.png` | Hillshade preview |

```bash
curl -s localhost:8000/api/model -H 'content-type: application/json' -d '{
  "source": "terrarium",
  "bbox": {"south": 35.28, "west": 138.65, "north": 35.45, "east": 138.82},
  "width_mm": 150, "exaggeration": 1.2, "resolution": 512, "name": "fuji"
}' | python -c 'import json,sys; j=json.load(sys.stdin); print(j["stl_url"], j["info"]["height_mm"])'
```

## How the scaling works

* **Plan scale** = `width_mm / ground_width_m`. The depth follows the true
  aspect ratio of the area (great-circle distances along the bbox centre lines).
* **Vertical** = plan scale × exaggeration, so `exaggeration = 1` is a true
  scale model. With `relief_mm` set, the vertical scale is chosen so the
  highest point sits exactly that far above the base, and the effective
  exaggeration is reported back.
* **Sea level** — below-0 m samples (bathymetry in the source data) are
  clamped to 0 by default so coastlines print as a flat plane.

## Layout

```
satprint/
  terrain.py   elevation sources (terrarium tiles, synthetic, file), scaling, hillshade
  mesh.py      heightmap → watertight solid, binary STL writer/reader, manifold check
  app.py       FastAPI backend (+ in-memory LRU of built models)
  cli.py       `satprint serve` / `satprint build`
  static/      front end: index.html, style.css, app.js (Leaflet + three.js from CDN)
tests/         pytest suite (offline; tile fetcher is faked)
```

## Tests

```bash
pytest -q
```

## Notes & limits

* Source resolution is ~30 m (SRTM) over most land, ~10 m at zoom 14 where
  available; small areas (< 2 km) will look blocky — use smoothing.
* Tile downloads are capped at 64 tiles per request to keep the server
  responsive; shrink the area or lower the resolution if you hit the cap.
* Built models live in memory (last 32); the download link is valid until the
  server restarts.
* Data: Terrain Tiles © Mapzen/AWS Open Data (SRTM, ASTER GDEM, GMTED2010,
  ETOPO1, NED, EU-DEM …). Imagery © Esri. Street map © OpenStreetMap.

## Docker

```bash
docker build -t satprint .
docker run -p 8000:8000 -v satprint-tiles:/root/.cache/satprint satprint
```
