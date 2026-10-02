# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Zenodo DOI**: the concept DOI `10.5281/zenodo.23094151`, which resolves to
  the newest archived release, in a README badge, the APA and BibTeX
  citations, and `CITATION.cff`.
- **Docker Hub image**: `egsuchanek/satprint`, built for `linux/amd64` and
  `linux/arm64`. The install docs now start from `docker run` on that image.
- **Container healthcheck** on `/api/health`, and a `.dockerignore`.

- **Roof shapes**: roofs tagged `roof:shape` dome, onion, cone or pyramidal
  are built with that shape, with the height from `roof:height` or
  `roof:levels`. Lanterns, cupolas and statues mapped on a dome stand in a
  hole cut in it. The OpenFreeMap tiles carry no roof tags, so a small Overpass
  query adds them; if it fails the roofs stay flat and the build reports
  `building_warning`.
- **Landmarks**: exact shapes for buildings that roof tags cannot describe,
  starting with the Sphere in Las Vegas, which printed as a cylinder.
- **Overture Maps building source** (`building_source: "overture"`,
  `--building-source overture`), behind the new `overture` extra. It adds
  Microsoft and Google footprints to OSM and keeps the roof tags. The Docker
  image includes it.

### Changed

- **The Docker image runs as an unprivileged user** (`satprint`, uid 1000), so
  the tile cache volume now mounts at `/home/satprint/.cache/satprint` instead
  of `/root/.cache/satprint`. Remove a volume made by the old image before
  reusing it.

## [0.1.0] - 2026-10-02

The first release.

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
- **Buildings.** OpenStreetMap footprints and heights become closed solids
  standing on the terrain, sunk 0.3 mm so they fuse with it when sliced, in
  both the STL and the GLB. In the GLB, roofs take the satellite texture.
  Where a building is mapped as `building:part` shapes, the parts replace the
  outline, so towers keep their setbacks. Overlapping footprints, which OSM
  has plenty of, are merged first: each overlap takes the tallest height
  covering it, so building solids touch but never pass through each other.
  Heights are in true proportion by default, with a multiplier. Areas are
  limited to 40 km² with buildings on.
- **Two building sources.** OpenFreeMap vector tiles are the default: zoom-14
  tiles from a CDN, no key, cached under `~/.cache/satprint/vtiles`. Midtown
  Manhattan is 6 tiles and about 10,000 buildings once overlapping footprints
  are merged, and builds with texture, multi-color 3MF and frame in about 6 s. The
  Overpass API is the fallback, for the latest OSM edits: 0.01° tiles on a
  fixed grid, each cached under `~/.cache/satprint/osm/tiles`, so a failed run
  keeps what it finished and overlapping areas share tiles. Busy answers are
  retried with backoff, and a server that times out is skipped for 10 minutes.
- **Multi-color 3MF** (`"multicolor": true`, `--3mf`). The terrain block is
  split along a water map into two closed solids that meet exactly, land and
  water; buildings and the border frame are the other parts. All are named,
  colored parts of one 3MF object, so a multi-material printer prints each in
  its own filament. Water is the OSM `water` layer of the OpenFreeMap tiles
  (sea, rivers, lakes; not swimming pools), plus the flattened sea. The 3MF
  reads in lib3mf's strict mode with no warnings, and every part is manifold.
  A Bambu-style `Metadata/model_settings.config` names the parts and puts
  land, water, buildings and border on filaments 1 to 4; Bambu Studio 2.08
  opens the file as one object with those parts and assignments.
- **Border frame** (`frame_mm` and `frame_height_mm`, `--frame` and
  `--frame-height`): a closed rectangular ring around the model, against its
  walls, 1 mm above the base by default. It goes into the STL, the GLB and the
  multi-color 3MF.
- **Textured GLB.** Esri World Imagery for the area is draped over the terrain
  and written as a GLB alongside the STL, in meters with Y up, so viewers show
  it at its real size. Elevation and imagery are cropped from the same Web
  Mercator window, so the texture lines up with the grid. The texture is at
  most 2048 px on its longer side. The GLB passes the Khronos glTF validator.
- **Place search** through Nominatim, limited to one request per second as its
  usage policy asks. A result becomes a square area: about 10 km around a
  natural feature, 1.5 to 5 km around anything else.
- **67 presets** in two groups: 37 cities and landmarks, which turn buildings
  on, and 30 mountains and landscapes.
- **Web app** (`satprint serve`, default port 7417): a Leaflet map with
  rectangle selection, search and presets, a three.js preview of the textured
  model, and STL, GLB and 3MF downloads. Builds run as background jobs, and
  the page shows the current stage with tile counts and elapsed time.
- **REST API**: `/api/model` builds synchronously; `POST /api/jobs` and
  `GET /api/jobs/{id}` build in the background and report progress.
  `/api/search`, `/api/presets` and `/api/upload` back the web app.
  Interactive docs are at `/docs`.
- **CLI**: `satprint build` with `--bbox`, `--file` or `--synthetic`, plus
  `--glb`, `--3mf`, `--frame`, `--buildings`, `--building-scale` and
  `--building-source`.
- **Documentation site** at https://suchanek.github.io/satprint/, built with
  MkDocs Material from `docs/` as quiltwright's is: guides for installation,
  the web app, multi-color printing, the CLI, the REST API, and data sources
  and limits, plus an API reference generated from the docstrings by
  mkdocstrings.
- **Logo**: an isometric relief block in the four print colors, in
  `docs/brand/`: the mark, and light and dark lockups with the name. The
  README shows the lockup for the reader's color scheme, and the web app and
  the docs site use the mark as their logo and favicon.
- **README in the fleet format**: Tests, Docs, Python, license, version,
  Poetry, pre-commit and ORCID badges, a multi-color printing guide, a
  data-sources table, and a Citation section with APA and BibTeX entries.
  `CITATION.cff` carries the same metadata, and `LICENSE` the MIT terms.
- **Packaging and tooling**: Poetry, as in the rest of the fleet, with the dev
  and docs tooling in optional groups and `poetry.lock` committed; Python 3.12
  or 3.13. A pre-commit configuration runs the standard file checks, ruff,
  detect-secrets, ty and pytest, and the ruff rule set is listed in
  `pyproject.toml` so pre-commit and CI check the same rules. `[tool.pycodekg]`
  and `[tool.dockg]` index settings and an `.mcp.json` serve the KG tools,
  which are global installs and not dependencies.
- **Workflows**: `tests.yml` (ruff, ty, pytest on Python 3.12 and 3.13, and a
  clean install of the built package that builds a model), `docs.yml`, which
  builds the site with `--strict` and deploys it to GitHub Pages, and
  `release.yml`, which creates the GitHub Release from `release-notes.md` on a
  `v*` tag for Zenodo to archive.
