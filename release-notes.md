# Release Notes -- v0.3.0

> Released: 2026-10-05

satprint 0.3.0 fixes two things that were visibly wrong in city prints: famous landmarks that came out as solid blobs, and bridges that were missing altogether. It also stops lakes from printing as raised plateaus, and gives the web page a light theme.

## What changed

**Landmarks are real shapes.** OpenStreetMap maps the Gateway Arch, the Eiffel Tower and the Space Needle as stacks of parts, and satprint extruded every part from the ground, so they printed as walls and columns. They are now built as meshes from published dimensions and placed in the model in place of the buildings underneath: an open Arch, an Eiffel Tower on four legs with archways, a Space Needle with its saucer. Christ the Redeemer in Rio is included too. At city scale its arms would be thinner than a printer can make, so the statue is enlarged in proportion, the way a symbol on a map is. Presets for St. Louis, Seattle and Rio are included.

**Bridges over water.** `--bridges`, `bridges: true` in the API and a checkbox in the web app add a deck across any bridge that crosses water, on evenly spaced piers with a short landing on each bank. OpenStreetMap has no deck heights, so the height is estimated from the banks, and the piers are not the real ones. There are no towers or cables. Bridges are part of the buildings solids, so they appear in the STL, the GLB and the 3MF.

**Lakes print flat.** Elevation data over water is noisy. A lake in a steep valley, such as the Lagoa in Rio, could read tens of meters above its shore and printed as a raised plateau with a cliff around it. Each lake, pond, reservoir and sea surface is now set to the lower shore of its own outline. Rivers are left alone.

**Web app.** The page has a light and a dark theme, following your system on the first visit. The header shows the version and links to the documentation. The preset list is labeled and sits above the search box, the app opens on the Gateway Arch, and dragging the map slides it under the selection rectangle so the area can be moved without redrawing it. Scroll and pinch zoom in the 3D preview now follow the gesture, which fixes a slow, many-pinch zoom on Retina Macs and in Safari. The documentation images were retaken.

## Upgrading

Nothing to do. Models of the same area can differ from 0.2.x: landmarks and bridges change the buildings, and lakes are lower. Bridges are off by default in the API and CLI, and the web app turns them on for city presets.

---

_Full changelog: [CHANGELOG.md](https://github.com/suchanek/satprint/blob/main/CHANGELOG.md)_
