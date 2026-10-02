# Release Notes -- v0.1.0

> Released: 2026-10-02

The first release. satprint turns real satellite elevation data into a solid
relief model scaled to your printer, from a place you pick on a map. One build
gives three files: a watertight STL for a one-color print, a multi-color 3MF
that a multi-material printer prints in up to four filaments, and a GLB with
satellite imagery draped over the terrain for viewing.

## What changed

**Terrain you can print.** Elevation comes from the public AWS Terrain Tiles
set with no API key. The model is a closed block, terrain on top, four walls
and a flat base, at true plan scale with vertical exaggeration or a fixed relief
height. Every edge is shared by exactly two outward-wound triangles, so slicers
load the STL without repair.

**Cities with their buildings.** OpenStreetMap footprints and heights become
closed solids standing on the terrain, read from OpenFreeMap's vector tiles,
with the Overpass API as a fallback. Towers mapped as building parts keep their
setbacks, and overlapping footprints are merged so no two solids pass through
each other. Midtown Manhattan comes to about 10,000 buildings and builds in
about 6 seconds.

**Four colors.** The multi-color 3MF splits the model into land, water,
buildings and an optional border frame, as named parts of one object. Water is
the sea, rivers and lakes from OpenStreetMap. Bambu Studio opens the file with
the parts already on filaments 1 to 4.

**A web app, a CLI and an API.** `satprint serve` runs a map with place search,
67 presets, a 3D preview of the textured model and a progress bar for each
build. `satprint build` does the same from the command line, and the REST API
behind the web app is open for scripting.

## Upgrading

Nothing to upgrade from. Install with `poetry install` or `pip install -e .`
from a clone, on Python 3.12 or 3.13, and run `satprint serve`. The
documentation is at https://suchanek.github.io/satprint/.

---

_Full changelog: [CHANGELOG.md](CHANGELOG.md)_
