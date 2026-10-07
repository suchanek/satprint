# Release Notes -- v0.5.0

> Released: 2026-10-07

satprint 0.5.0 makes the multi-color 3MF easier to print on a Bambu printer. It came out of slicing the Boston Downtown and Gateway Arch presets on an A1 with a 0.2 mm nozzle. The 3MF now needs four filaments instead of five, so it fits an AMS lite, it opens centered on the plate, and its base prints in one filament, which cuts the number of filament changes by about 45 percent.

## What changed

**Four filaments.** Parts of one color now share a filament: land is filament 1, water 2, buildings 3 and roads 4. Bridges moved from the buildings part to the roads part, so they print gray with the roads instead of white with the buildings. The border prints in the land filament. Each is still its own part, so any of them can take another filament in the slicer.

**Centered on the plate.** The model used to be written at the origin, with the border reaching to -3 mm, so a slicer that kept that position put a corner off the plate. The 3MF now centers the model on a 256 mm plate for the Bambu A1, P1 and X1, or on a 180 mm plate for the A1 mini. Choose the plate under **3MF plate** in the web app or with `--plate` on the command line.

**Fewer filament changes.** The water part ran from the bottom of the base to the water surface, and the gray border ran the full height of the base, so every layer from the first one needed two or three filament changes. The water is now a 0.6 mm skin with the land filament under it, and the border is green, so the bottom of the print needs no changes at all. On Boston Downtown the count drops from 114 changes to 63 at 0.1 mm layers, and from 192 to 106 at 0.06 mm. Where water reaches the edge of the model, the side shows a thin blue band over green instead of a blue column.

**Troubleshooting for multi-color prints.** The multi-color guide has a new section on what to do when slicing fails or warns: the prime tower running past the plate edge (common on 0.2 mm nozzle presets, where thin layers make the tower larger), floating-cantilever warnings on bridges, and where the time goes in a long print.

## Upgrading

Nothing to do, but 3MF files come out different from 0.4.1: filament numbers, the part that holds the bridges, the border color and the shape of the water part have all changed. Generate a 3MF again rather than reusing slicer settings saved against an old one. STL and GLB output is unchanged.

---

_Full changelog: [CHANGELOG.md](https://github.com/suchanek/satprint/blob/main/CHANGELOG.md)_
