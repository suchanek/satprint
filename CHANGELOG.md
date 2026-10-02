# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Terrain models from satellite elevation.** Pick an area and get a
  watertight STL: the terrain surface on top, four walls and a flat base, with
  every edge shared by exactly two outward-wound triangles. Elevation comes
  from the AWS Terrain Tiles set (SRTM, ASTER, GMTED and others), with no API
  key, cached under `~/.cache/satprint/tiles`. Uploaded PNG, TIFF and GeoTIFF
  heightmaps and a procedural demo terrain are also supported.
- **Print settings**: true plan scale, vertical exaggeration or a fixed relief
  height, base thickness, sea-level flattening, Gaussian smoothing and grids up
  to 1024 columns. Each build reports model size, scale, triangle count,
  volume and an estimated PLA weight.
- **Textured GLB.** Esri World Imagery for the area is draped over the terrain
  and written as a GLB alongside the STL, in meters with Y up, so viewers show
  it at its real size. Elevation and imagery are cropped from the same Web
  Mercator window, so the texture lines up with the grid. The texture is at
  most 2048 px on its longer side. The GLB passes the Khronos glTF validator.
- **Buildings.** OpenStreetMap footprints and heights become closed solids
  standing on the terrain, sunk 0.3 mm so they fuse with it when sliced, in
  both the STL and the GLB. In the GLB, roofs take the satellite texture.
  Where a building is mapped as `building:part` shapes, the parts replace the
  outline, so towers keep their setbacks. Heights are in true proportion by
  default, with a multiplier. Areas are limited to 40 km² with buildings on.
- **Two building sources.** OpenFreeMap vector tiles are the default: zoom-14
  tiles from a CDN, no key, cached under `~/.cache/satprint/vtiles`. Midtown
  Manhattan is 6 tiles and about 11,000 buildings, built in about 3 s. The
  Overpass API is the fallback, for the latest OSM edits: 0.01° tiles on a
  fixed grid, each cached under `~/.cache/satprint/osm/tiles`, so a failed run
  keeps what it finished and overlapping areas share tiles. Busy answers are
  retried with backoff, and a server that times out is skipped for 10 minutes.
- **Place search** through Nominatim, limited to one request per second as its
  usage policy asks. A result becomes a square area: about 10 km around a
  natural feature, 1.5 to 5 km around anything else.
- **67 presets** in two groups: 37 cities and landmarks, which turn buildings
  on, and 30 mountains and landscapes.
- **Web app** (`satprint serve`, default port 7417): a Leaflet map with
  rectangle selection, search and presets, a three.js preview of the textured
  model, and STL and GLB downloads. Builds run as background jobs, and the page
  shows the current stage with tile counts and elapsed time.
- **REST API**: `/api/model` builds synchronously; `POST /api/jobs` and
  `GET /api/jobs/{id}` build in the background and report progress.
  `/api/search`, `/api/presets` and `/api/upload` back the web app.
  Interactive docs are at `/docs`.
- **CLI**: `satprint build` with `--bbox`, `--file` or `--synthetic`, plus
  `--glb`, `--buildings`, `--building-scale` and `--building-source`.
- **Development tooling**: a pre-commit configuration running the standard
  file checks, ruff, detect-secrets, ty and pytest; `[tool.pycodekg]` and
  `[tool.dockg]` index settings and an `.mcp.json` for the KG tools, which are
  global installs and not dependencies.
