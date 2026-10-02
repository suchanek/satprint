# Data sources and limits

## Sources

| Data | Source | Cache |
|---|---|---|
| Elevation | [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) | `~/.cache/satprint/tiles` |
| Imagery (GLB texture) | Esri World Imagery | `~/.cache/satprint/imagery` |
| Buildings and water | [OpenFreeMap](https://openfreemap.org) vector tiles, zoom 14, rebuilt from OSM about weekly | `~/.cache/satprint/vtiles` |
| Buildings, fallback | [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API), 0.01° tiles | `~/.cache/satprint/osm/tiles` |
| Place search | [Nominatim](https://nominatim.org), one request per second | in memory |

None of them needs an API key. Every tile is cached on disk, so a second build
of the same area downloads nothing.

### Buildings

- A building with no `height` or `building:levels` tag gets 8 m.
- Where OSM maps a building as `building:part` shapes, the parts replace its
  outline, so towers keep their setbacks.
- Parts are extruded from the ground, ignoring `min_height`, so nothing floats.
- Overlapping footprints are merged first: each overlap takes the tallest
  height covering it, so the solids touch but never pass through each other.
- Buildings sink 0.3 mm into the terrain so they fuse with it when sliced.
- A building that crosses a vector-tile edge arrives as two solids that meet at
  the edge.

### Overpass

Choose Overpass with "Building data" in the web app or
`--building-source overpass` on the CLI, for the latest OSM edits. The default
"auto" uses it only if OpenFreeMap fails. Its tiles download two at a time,
busy answers are retried with backoff, and a server that times out is skipped
for 10 minutes. When some tiles fail, the rest stay cached, so generating again
fetches only the missing ones.

### Imagery

The texture is at most 2048 px on its longer side. Esri's terms of use govern
the imagery. Set `"texture": false` to skip it.

## Limits

- Source resolution is about 30 m (SRTM) over most land and about 10 m at zoom
  14 where available, so areas smaller than about 2 km look blocky. Smoothing
  helps.
- Elevation and imagery downloads are capped at 64 tiles per request. Shrink
  the area or lower the resolution if you hit the cap.
- Buildings are limited to areas up to 40 km², and very small footprints are
  dropped.
- The multi-color split works one grid cell at a time, so a river narrower than
  a cell does not show.
- The GLB is for viewing. FDM slicers ignore textures, so print the STL or the
  3MF.
- Built models live in memory, the last 32. A download link is valid until the
  server restarts.

## Attribution

Terrain Tiles © Mapzen and AWS Open Data (SRTM, ASTER GDEM, GMTED2010, ETOPO1,
NED, EU-DEM and others). Imagery © Esri, Maxar, Earthstar Geographics. Street
map, buildings, water and search © OpenStreetMap contributors, under the ODbL.
