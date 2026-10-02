# satprint

**Satellite terrain to 3D-printable models.**

*Eric G. Suchanek, PhD -- Flux-Frontiers*

Pick a place on a map and satprint turns its real elevation data into a solid
relief model scaled to your printer: the terrain on top, four walls and a flat
base. You get three files from the same model:

- a watertight **STL** of the terrain and its buildings, for a one-color print;
- a **multi-color 3MF** that splits the model into land, water, buildings and a
  border frame, one filament each, for a multi-material printer;
- a **GLB** with satellite imagery draped over the terrain and the roofs, for
  viewing in Blender, macOS Quick Look or a web viewer.

![The satprint web app](screenshot.png)

Mount Fuji from real elevation tiles, 120 mm wide, 1.2x exaggeration:

![Mount Fuji preview](fuji-preview.png)

## What it does

- **Real elevation, no API key.** Elevation comes from the public AWS Terrain
  Tiles set (SRTM, ASTER, GMTED and others). You can also upload a heightmap or
  use a procedural demo terrain that needs no network.
- **Print-aware output.** True plan scale, vertical exaggeration or a fixed
  relief height, base thickness, sea-level flattening, smoothing and grids up
  to 1024 columns. Each build reports the model size, scale, triangle count,
  volume and an estimated PLA weight.
- **Watertight by construction.** Every edge is shared by exactly two
  outward-wound triangles, so slicers load the STL without repair.
- **Buildings** from OpenStreetMap, as closed solids standing on the terrain.
  Towers keep their setbacks, and overlapping footprints are merged so no two
  solids pass through each other.
- **Four colors.** Land, water, buildings and a border frame are separate parts
  of one 3MF object, already on filaments 1 to 4 in Bambu Studio.
- **Search and presets.** Search any place by name, or pick one of 67 presets:
  37 cities and landmarks and 30 mountains and landscapes.

## Where to go next

- [Install satprint](install.md) and [run the web app](usage.md).
- [Print in several colors](multicolor.md) on a multi-material printer.
- Script it with the [CLI](cli.md) or the [REST API](rest-api.md).
- Read where the data comes from, and the limits, in
  [Data sources and limits](data-sources.md).
