# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Poetry packaging**, as in the rest of the fleet: `poetry-core` builds the
  package, the dev and docs tooling are optional Poetry groups, and
  `poetry.lock` records the versions. `requirements.txt` is gone. Python 3.12
  or 3.13 is required. The ruff rule set is listed in `pyproject.toml`, so the
  pinned pre-commit ruff and the newest ruff in CI check the same rules.

### Added

- **Border frame** (`frame_mm` and `frame_height_mm`, `--frame` and
  `--frame-height`): a closed rectangular ring around the model, against its
  walls, 1 mm above the base by default. It goes into the STL and GLB, and
  into the multi-color 3MF as a fourth part, `border`, on filament 4.
- **Multi-color 3MF** (`"multicolor": true`, `--3mf`). The terrain block is
  split along a water map into two closed solids that meet exactly, land and
  water, and buildings are a third part. The three are named, colored parts
  of one 3MF object, so a multi-material printer prints each in its own
  filament. Water is the OSM `water` layer of the OpenFreeMap tiles (sea,
  rivers, lakes; not swimming pools), plus the flattened sea. The 3MF reads
  in lib3mf's strict mode with no warnings, and every part is manifold. A
  Bambu-style `Metadata/model_settings.config` names the parts and puts
  land, water and buildings on filaments 1, 2 and 3; Bambu Studio 2.08 opens
  the file as one object with those three parts and assignments.

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
  outline, so towers keep their setbacks. Overlapping footprints, which OSM
  has plenty of, are merged first: each overlap takes the tallest height
  covering it, so building solids touch but never pass through each other. Heights are in true proportion by
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
- **Logo**: an isometric relief block in the four print colors (land, water,
  buildings and peaks, and the border frame as its sides), in
  `docs/brand/`: the mark, and light and dark lockups with the name. The
  README shows the lockup for the reader's color scheme, and the web app uses
  the mark as its favicon and header logo.
- **README in the fleet format**: a badge row (Python, license, version,
  pre-commit, ORCID), a multi-color printing guide with the filament table,
  a data-sources table, and a Citation section with APA and BibTeX entries.
  `CITATION.cff` carries the same metadata, and `LICENSE` holds the MIT terms
  `pyproject.toml` already declared.
- **Documentation site** at https://suchanek.github.io/satprint/, built with
  MkDocs Material from `docs/` as quiltwright's is: guides for installation,
  the web app, multi-color printing, the CLI, the REST API, and data sources
  and limits, plus an API reference generated from the docstrings by
  mkdocstrings. The `docs.yml` workflow builds it with `--strict` and deploys
  it to GitHub Pages.
- **Workflows**: `tests.yml` (ruff, ty, pytest on Python 3.12 and 3.13, and a
  clean install of the built package that builds a model), `docs.yml`, and
  `release.yml`, which creates the GitHub Release from `release-notes.md` on a
  `v*` tag for Zenodo to archive.
- **Development tooling**: a pre-commit configuration running the standard
  file checks, ruff, detect-secrets, ty and pytest; `[tool.pycodekg]` and
  `[tool.dockg]` index settings and an `.mcp.json` for the KG tools, which are
  global installs and not dependencies.
