# CLI reference

satprint has two commands: `satprint serve` runs the web app and
`satprint build` builds a model.

## satprint serve

```bash
satprint serve [--host 127.0.0.1] [--port 7417] [--reload]
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Address to listen on; `0.0.0.0` for every interface |
| `--port` | `7417` | Port to listen on |
| `--reload` | off | Restart when the code changes, for development |

## satprint build

```bash
satprint build (--bbox S W N E | --file FILE | --synthetic) [options] -o model.stl
```

Bounds are `SOUTH WEST NORTH EAST` in decimal degrees. The command writes the
STL, prints a JSON summary (elevation range, scale, triangle count, volume and a
watertight check) and writes any extra files you ask for.

### Source

| Option | Meaning |
|---|---|
| `--bbox S W N E` | Satellite elevation for this area |
| `--file FILE` | A PNG, TIFF or GeoTIFF heightmap, in meters |
| `--synthetic` | Procedural demo terrain, offline |
| `--ground-width M` | Real-world width in meters, for `--file` |
| `--seed N` | Seed for `--synthetic` |

### Print settings

| Option | Default | Meaning |
|---|---|---|
| `--resolution N` | 256 | Grid columns, 32 to 1024 |
| `--width MM` | 100 | Model width |
| `--base MM` | 3 | Base thickness |
| `--exaggeration X` | 1.5 | Vertical exaggeration |
| `--relief MM` | | Fixed relief height; overrides `--exaggeration` |
| `--smoothing PX` | 0 | Gaussian smoothing, in grid pixels |
| `--keep-bathymetry` | off | Keep below-sea-level data instead of flattening it |
| `--frame MM` | 0 | Border frame width around the model; 0 for none |
| `--frame-height MM` | base + 1 | Frame height |
| `--name NAME` | `terrain` | Model name, stored in the files |

### Buildings

These need `--bbox`.

| Option | Default | Meaning |
|---|---|---|
| `--buildings` | off | Add OpenStreetMap buildings |
| `--building-scale X` | 1 | Building height multiplier; 1 is true proportion |
| `--building-source` | `openfreemap` | `openfreemap` (fast, rebuilt weekly) or `overpass` (latest edits, slower) |

### Output

| Option | Meaning |
|---|---|
| `-o`, `--output FILE` | The STL, `terrain.stl` by default |
| `--3mf FILE` | Also write the multi-color 3MF; needs `--bbox` |
| `--glb FILE` | Also write the textured GLB; needs `--bbox` |
| `--preview FILE` | Also write a hillshade PNG |

## Examples

```bash
# Matterhorn, 120 mm wide, 2x exaggeration
satprint build --bbox 45.93 7.58 46.02 7.72 --width 120 --exaggeration 2 -o matterhorn.stl

# Fixed 15 mm relief, smoothed, with a hillshade preview PNG
satprint build --bbox 36.02 -112.25 36.20 -111.95 --relief 15 --smoothing 1 \
               --preview canyon.png -o grand-canyon.stl

# Midtown Manhattan with buildings, plus a textured GLB
satprint build --bbox 40.7414 -73.9997 40.7684 -73.9683 --width 150 \
               --buildings --glb midtown.glb -o midtown.stl

# Venice as a four-color 3MF: land, water, buildings and a 5 mm border frame
satprint build --bbox 45.43 12.32 45.446 12.343 --buildings --frame 5 \
               --3mf venice.3mf -o venice.stl

# From your own DEM in meters, 12 km across
satprint build --file dem.tif --ground-width 12000 -o dem.stl
```
