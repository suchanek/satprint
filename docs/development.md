# Development

## Set up

```bash
poetry install --with dev
pre-commit install
```

## Test

```bash
pytest -q
```

The suite runs offline: every network client is replaced by a fake that serves
generated tiles, so it needs no network and takes under a second.

## Pre-commit hooks

Every commit runs the standard file checks, ruff (lint and format),
detect-secrets, ty and pytest. The local hooks call `.venv/bin/ty` and
`.venv/bin/pytest` directly, so an inherited `VIRTUAL_ENV` from another
project cannot redirect them.

## Build the docs

```bash
poetry install --with docs
mkdocs serve                     # http://127.0.0.1:8000, rebuilds on save
mkdocs build                     # into site/
```

The API reference pages are generated from the docstrings by mkdocstrings, so
they follow the code. The `docs.yml` workflow publishes the site to GitHub
Pages on every push to `main` that touches `docs/`, `satprint/` or
`mkdocs.yml`.

## Layout

```
satprint/
  terrain.py    elevation sources (terrain tiles, synthetic, file), imagery, scaling, hillshade
  mesh.py       heightmap to watertight solid, land/water split, frame, STL, GLB and 3MF writers
  buildings.py  OSM buildings to closed solids on the terrain, roof shapes
  landmarks.py  exact shapes for a few landmarks
  overture.py   Overture Maps building source (overture extra)
  water.py      water map and the multi-color 3MF parts
  osm.py        OpenFreeMap, Overpass and Nominatim clients
  presets.py    named example areas
  app.py        FastAPI backend, background jobs, in-memory model store
  cli.py        satprint serve / satprint build
  static/       web app: index.html, style.css, app.js (Leaflet and three.js from CDNs)
tests/          pytest suite, offline
docs/           this site
```

## Knowledge graphs

`pycodekg` and `dockg` index the repo for agents through `.mcp.json`. They are
global tools, not dependencies, and `[tool.pycodekg]` and `[tool.dockg]` in
`pyproject.toml` set what they index.
