# Print in several colors

The multi-color 3MF holds up to four parts of one object, in this filament
order:

| Filament | Part | Display color |
|---|---|---|
| 1 | `land` | green |
| 2 | `water` | blue |
| 3 | `buildings` | white |
| 4 | `border` | dark gray |

Bridges share the `buildings` part. Parts without geometry are left out:
there is no `buildings` part without buildings or bridges, and no `border`
part without a frame.

## Make one

In the web app, check **Multi-color 3MF**, and set a **Border frame** width if
you want the fourth part. Generate, then click **Download multi-color 3MF**.

From the command line:

```bash
satprint build --bbox 45.43 12.32 45.446 12.343 --buildings --frame 5 \
               --3mf venice.3mf -o venice.stl
```

## Print it on a Bambu printer

1. In Bambu Studio, set up four filaments in the left sidebar, or sync them
   from the AMS.
2. Open the 3MF. Bambu Studio reports that it loads "geometry only", as it does
   for any 3MF it did not write. The part names and filaments still come
   through.
3. To check or change a part's filament, open the object list: in the left
   sidebar, under **Process**, click **Objects** and expand the model.
4. Slice. The preview shows each part in its filament's color, and a prime
   tower for the filament changes.

To keep your filament choices and print settings, save the result with
**File > Save Project As**. That project opens without the "geometry only"
message.

Other slicers open the same parts with every part on filament 1. Assign the
filaments by hand.

## How the parts are made

- **Water** is the sea, river and lake shapes from the OpenStreetMap `water`
  layer, plus the flattened sea. Swimming pools are left out, since many are on
  roofs.
- **Land and water** are the terrain block split along that water map, one
  grid cell at a time. The two solids meet exactly and fill the same block
  together. A river narrower than one grid cell does not show, so raise the
  resolution to keep it.
- **Buildings** are the same solids as in the STL, sunk 0.3 mm into the
  terrain. Slicers resolve that overlap. Bridges are in this part too, so a
  bridge prints in the buildings filament.
- **Border** is a closed rectangular ring against the walls of the block, 1 mm
  above the base by default.
