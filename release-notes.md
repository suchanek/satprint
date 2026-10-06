# Release Notes -- v0.4.0

> Released: 2026-10-06

satprint 0.4.0 adds roads. Until now a city print showed buildings standing on bare ground, and the street grid could only be read from the gaps between them. Roads now print as their own raised strips that follow the terrain, in the STL, the GLB and the multi-color 3MF, where they get a filament of their own.

## What changed

**Roads.** `--roads`, `roads: true` in the API and a Roads checkbox in the web app draw the OpenStreetMap road network from the same tiles as the buildings. A real street is a fraction of a nozzle wide at print scale, so every road is drawn at least 0.8 mm wide and 0.4 mm high. Which roads are drawn depends on the model's scale: a region shows its motorways and main roads, a city adds its minor streets, and an area under about a kilometer across adds service roads and paths. `--road-detail` and the Road detail selector can instead ask for major roads only or for everything. Roads stop at water, at buildings and at bridge landings, and tunnels, rail and ferries are left out. Each connected road network is one closed solid that stays watertight with the terrain, buildings and bridges around it.

**Roads follow hills.** Each strip is cut along the terrain mesh, so it climbs and descends with the ground instead of cutting into a hillside or floating over a valley. On fine grids the strips are cut on a coarser lattice to keep the triangle count down, and they are raised by however far the terrain strays from that lattice, so they still clear the ground everywhere. On a 1024-column model of San Francisco this cut the roads from 2.1 million triangles to 373 thousand, and the whole build from 73 seconds to 35.

**The web page reloads cleanly.** The page and its files are now served so that the browser checks for a newer copy on every load. After an upgrade, a plain reload shows the new version instead of a cached old one.

## Upgrading

Nothing to do. Roads are off by default in the API, the CLI and the web app, so existing builds are unchanged. A browser that cached the 0.3.0 page needs one hard reload to pick up the Roads controls; after that, plain reloads are enough. The multi-color 3MF can now have five parts: a model with roads puts them on filament 4 and the border on filament 5.

---

_Full changelog: [CHANGELOG.md](https://github.com/suchanek/satprint/blob/main/CHANGELOG.md)_
