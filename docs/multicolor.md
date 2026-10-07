# Print in several colors

The multi-color 3MF holds up to five parts of one object on up to four
filaments, so it fits an AMS lite:

| Filament | Part | Display color |
|---|---|---|
| 1 | `land` | green |
| 2 | `water` | blue |
| 3 | `buildings` | white |
| 4 | `roads` | dark gray |
| 1 | `border` | green |

The border prints in the land filament, and bridges are in the `roads` part. Parts without geometry are left out, and the filaments after them move
up one: there is no `buildings` part without buildings, no `roads` part
without roads or bridges, and no `border` part without a frame.

## Make one

In the web app, check **Multi-color 3MF**, check **Add roads** if you want the
roads part, and set a **Border frame** width if you want the border part. Pick your printer's
plate under **3MF plate**. Generate, then click **Download multi-color 3MF**.

From the command line:

```bash
satprint build --bbox 45.43 12.32 45.446 12.343 --buildings --frame 5 \
               --3mf venice.3mf -o venice.stl
```

Add `--plate 180` for an A1 mini.

## Print it on a Bambu printer

1. In Bambu Studio, set up one filament per color, up to four, in the left
   sidebar, or sync them from the AMS.
2. Open the 3MF. Bambu Studio reports that it loads "geometry only", as it does
   for any 3MF it did not write. The part names and filaments still come
   through. The model lands centered on the plate you chose: 256 mm (A1, P1, X1) or
   180 mm (A1 mini).
3. To check or change a part's filament, open the object list: in the left
   sidebar, under **Process**, click **Objects** and expand the model.
4. Slice. The preview shows each part in its filament's color, and a prime
   tower for the filament changes.

To keep your filament choices and print settings, save the result with
**File > Save Project As**. That project opens without the "geometry only"
message.

Other slicers open the same parts with every part on filament 1. Assign the
filaments by hand.

## If slicing fails or warns

**"A G-code path goes beyond plate boundaries."** The prime tower is off the
plate. A multi-color print builds this small block beside the model: at each
filament change the printer primes the new filament on it, so the model
gets clean color. The tower holds the same amount of filament on every layer,
so the thinner the layers, the larger it gets, and on a 0.2 mm nozzle preset
Bambu Studio's default spot can put it past the plate edge. In **Prepare**,
drag the tower into open plate space away from the edges and slice again. If
it does not fit anywhere, raise **Prime tower width** or lower **Prime volume**
and the tower brim under **Process > Others**.

**"Floating cantilever."** Bambu Studio found overhangs with nothing under
them, usually bridge decks between piers. Short spans print fine without
supports. To shorten long ones, build again with a smaller **Bridge piers**
spacing, or turn on tree supports.

**Print time.** Each filament change takes about a minute on an A1, and a
print has up to three per layer near the water line. Most of a long
estimate is still the printing itself: a 0.2 mm nozzle at 0.06 mm layers can
take a day on a 120 mm city. The 0.10 mm preset on the same nozzle takes far
less and still shows the buildings well.

## How the parts are made

- **Water** is the sea, river and lake shapes from the OpenStreetMap `water`
  layer, plus the flattened sea. Swimming pools are left out, since many are on
  roofs.
- **Land and water** are the terrain block split along that water map, one
  grid cell at a time. The water is a skin 0.6 mm thick, and the land
  filament fills the base under it, so the lower layers print in one color
  with no filament changes for water. The two solids meet exactly and fill
  the same block together. A river narrower than one grid cell does not show, so raise the
  resolution to keep it.
- **Buildings** are the same solids as in the STL, sunk 0.3 mm into the
  terrain. Slicers resolve that overlap.
- **Roads** are the same raised strips as in the STL, sunk 0.3 mm into the
  terrain like the buildings. Bridges are in this part too, so a bridge
  prints in the road gray.
- **Border** is a closed rectangular ring against the walls of the block, 1 mm
  above the base by default.
