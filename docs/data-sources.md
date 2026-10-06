# Data sources and limits

## Sources

| Data | Source | Cache |
|---|---|---|
| Elevation | [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) | `~/.cache/satprint/tiles` |
| Imagery (GLB texture) | Esri World Imagery | `~/.cache/satprint/imagery` |
| Buildings, bridges, roads and water | [OpenFreeMap](https://openfreemap.org) vector tiles, zoom 14, rebuilt from OSM about weekly | `~/.cache/satprint/vtiles` |
| Buildings, fallback; roof shapes | [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API), 0.01° tiles | `~/.cache/satprint/osm` |
| Buildings, optional | [Overture Maps](https://overturemaps.org) GeoParquet on S3, per release | `~/.cache/satprint/overture` |
| Place search | [Nominatim](https://nominatim.org), one request per second | in memory |

None of them needs an API key. Every tile is cached on disk, so a second build
of the same area downloads nothing.

### Buildings

- A building with no `height` or `building:levels` tag gets 8 m.
- Where OSM maps a building as `building:part` shapes, the parts replace its
  outline, so towers keep their setbacks.
- Parts are extruded from the ground, ignoring `min_height`, so nothing floats.
- Roofs tagged `roof:shape` dome, onion, cone or pyramidal get that shape.
  The roof height comes from `roof:height`, then `roof:levels` at 3 m each,
  then the radius of the footprint, which makes a dome a hemisphere. Other
  shapes, such as gabled and hipped, are flat; at print scale a house roof is a
  fraction of a millimeter.
- A shaped roof needs a footprint without holes that can see all its corners
  from its center, and must stand at least 0.3 mm tall in the model. Otherwise
  the building is flat at its full height.
- A shaped roof starts no lower than the flat roofs at least half its size that
  it overlaps, so a dome set into a taller wing does not sit in a pit.
- Smaller parts standing on a shaped roof, such as a lantern, cupola or statue
  on a dome, keep their place: the roof stops at the highest ring that still
  encloses them, and they stand in the hole that leaves. A part that would not
  reach that ring ends up inside the roof.
- Of two overlapping shaped roofs that are not standing on each other, the
  taller keeps its shape and the other is flat.
- The OpenFreeMap tiles carry no roof tags, so a small Overpass query fetches
  the shaped buildings in the area and they replace their copies from the
  tiles. If that query fails, the roofs are flat and the build reports
  `building_warning`.
- A few landmarks that roof tags cannot describe get exact shapes from
  published dimensions, matched by their Wikidata ID or OSM ID
  (`satprint/landmarks.py`). So far: the Sphere in Las Vegas, a 157 m sphere
  cut by the ground at 112 m.
- Landmarks that a prism cannot express supply a closed triangle mesh and are
  placed by a fixed position instead: the Gateway Arch in St. Louis, the Eiffel
  Tower in Paris (four legs, open between them, up to the second platform) and
  the Space Needle in Seattle and Christ the Redeemer in Rio de Janeiro (OSM
  maps the statue as a single point, so it is placed by that point). They replace the buildings OSM maps under
  them, and only when the whole landmark lies inside the area; a landmark cut
  by the edge keeps the OSM buildings. The Arch and the Needle honor the
  minimum feature size, so thin parts are thickened on small prints; the
  Eiffel Tower keeps its true proportions. Christ the Redeemer is enlarged in
  proportion on city-sized prints, until its arms reach the minimum, so it
  stands taller than life, as a symbol on a map does. The Arch, the Needle's
  saucer and the Eiffel Tower's archways are overhangs that may want slicer
  supports.
- Overlapping footprints are merged first: each overlap takes the tallest
  height covering it, so the solids touch but never pass through each other.
- Buildings sink 0.3 mm into the terrain so they fuse with it when sliced.
- A building that crosses a vector-tile edge arrives as two solids that meet at
  the edge.

### Bridges

Bridges come from the same OpenFreeMap tiles as buildings: `man_made=bridge`
outlines, and road, rail and path lines tagged as bridges, which are drawn as
strips of a width per class (a motorway 12 m, a path 3 m, never narrower than
0.8 mm in the model). Only bridges over water from the tiles' water layer are
built.

- Each bridge is cut to the water plus a landing of about 30 m, at least 1 mm,
  on each bank, so long approaches and interchanges are left out.
- OSM has no deck heights. A landing is flat at the highest ground under it
  plus 0.2 mm. Over the water the deck height blends the landings' heights by
  distance, and rises to at least 1 mm above the water, easing in over 3 mm
  from the banks, so a low bridge arches.
- The span is a thin deck slab 0.6 mm thick, open underneath, held up by piers
  1 mm thick at even spacing, no farther apart than `--bridge-piers` (20 mm by
  default). They are not the real piers. There are no towers or cables.
- Every part is a closed solid that touches the others only along shared
  vertical faces. Landings and piers sink 0.3 mm into the terrain. Bridges
  join the buildings solids, in the STL, the GLB and the 3MF.
- A deck between its piers is an overhang that may want slicer supports.

### Roads

Roads come from the same OpenFreeMap tiles, the `transportation` layer's road
and path lines. Tunnels are left out, and so are rail, ferries and roads under
construction.

- Each line is a strip of its class width, the same widths as bridge decks,
  never narrower than 0.8 mm in the model. At print scale a 7 m street is a
  fraction of a nozzle wide, so most roads are drawn wider than they are.
- With `auto` detail, a class is drawn once the model scale makes its streets
  print a few strip widths apart: motorways, trunk and primary roads always,
  secondary from about 0.003 mm per meter, tertiary from 0.008, minor streets
  from 0.03 (a city a few kilometers across at 100 mm), and service roads,
  tracks and paths from 0.1. `major` draws motorway to secondary, and `all`
  draws every class.
- The strips are joined, water and building footprints are cut out, and the
  result is cut along the terrain mesh's triangles, or, on a fine grid, along
  a lattice of every few grid nodes about 0.6 mm apart, so roads do not add
  as many triangles as the terrain. Between lattice nodes the terrain strays
  from the lattice; the lattice is made finer until it strays no more than
  0.3 mm under the roads, and the roads are raised and sunk by that much.
  The top is at least 0.4 mm above the terrain and the floor at least 0.3 mm
  below it everywhere. Each connected road network is one closed solid.
- A road over water is cut at the water; turn on bridges to span it.

### Overpass

Choose Overpass with "Building data" in the web app or
`--building-source overpass` on the CLI, for the latest OSM edits. The default
"auto" uses it only if OpenFreeMap fails. Its tiles download two at a time,
busy answers are retried with backoff, and a server that times out is skipped
for 10 minutes. When some tiles fail, the rest stay cached, so generating again
fetches only the missing ones.

### Overture Maps

Choose Overture with "Building data" in the web app or
`--building-source overture` on the CLI. [Overture
Maps](https://overturemaps.org) merges OSM with Microsoft and Google building
footprints, so it finds buildings OSM lacks, and it keeps the OSM height and
roof tags. It needs the `overture` extra:

```bash
pip install "satprint[overture]"     # or: poetry install --extras overture
```

The extra brings pyarrow, about 190 MB; the Docker image includes it.
Overture's STAC index picks the GeoParquet files on S3 that cover the area, so
a query takes a few seconds. Results are cached per Overture release. "auto"
never picks Overture.

### Imagery

The texture is at most 2048 px on its longer side. Esri's terms of use govern
the imagery. Set `"texture": false` to skip it.

## Limits

- Source resolution is about 30 m (SRTM) over most land and about 10 m at zoom
  14 where available, so areas smaller than about 2 km look blocky. Smoothing
  helps.
- Elevation and imagery downloads are capped at 64 tiles per request. Shrink
  the area or lower the resolution if you hit the cap.
- Buildings, bridges and roads are limited to areas up to 40 km², and very
  small footprints are dropped.
- Bridges over water only. A bridge over a road, a railway or a valley is
  missing, and so is one whose water is not in the tiles' water layer.
- The multi-color split works one grid cell at a time, so a river narrower than
  a cell does not show.
- The GLB is for viewing. FDM slicers ignore textures, so print the STL or the
  3MF.
- Built models live in memory, the last 32. A download link is valid until the
  server restarts.

## Attribution

Terrain Tiles © Mapzen and AWS Open Data (SRTM, ASTER GDEM, GMTED2010, ETOPO1,
NED, EU-DEM and others). Imagery © Esri, Maxar, Earthstar Geographics. Street
map, buildings, bridges, roads, water and search © OpenStreetMap contributors, under the ODbL.
Overture buildings © Overture Maps Foundation and OpenStreetMap contributors,
under the ODbL.
