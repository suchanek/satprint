# The web app

Start the server and open it in a browser:

```bash
satprint serve                   # http://127.0.0.1:7417
satprint serve --host 0.0.0.0 --port 7417
```

After you change the code, restart `satprint serve` to pick up the change.

## Build a model

1. **Choose an area.** Search for a place, pick a preset, click **Draw
   rectangle** and drag on the map, or type the bounds. The hint line shows the
   real size of the area and the model it makes.
2. **Set the print.** Model width, base thickness, vertical exaggeration (1.5x
   to 3x reads well for most landscapes; 1x is true scale) or a fixed relief
   height. Resolution sets the grid columns: 256 is fine for FDM, 512 to 1024
   for resin. Smoothing hides sensor noise on flat areas.
3. **Add the extras you want.**
    - **Add buildings** for a city. City presets turn it on.
    - **Multi-color 3MF** for a multi-material printer, and a **Border frame**
      width for a rim around the model.
    - **Drape satellite imagery** for the textured GLB. It is on by default.
4. **Generate.** A progress bar shows each stage with tile counts. The
   textured model appears in the 3D preview. Download the STL to print in one
   color, the 3MF to print in several, or the GLB to view.

## Search

Type a place and press Enter. Results come from OpenStreetMap's Nominatim
service. A result always becomes a square area: about 10 km around a natural
feature such as a peak, and 1.5 to 5 km around anything else, a size where
buildings still work.

## How the scaling works

- **Plan scale** is `width_mm / ground_width_m`. The depth follows the true
  aspect ratio of the area, from great-circle distances along the center lines
  of the bounding box.
- **Vertical scale** is the plan scale times the exaggeration, so
  `exaggeration = 1` is a true-scale model. With a fixed relief height, the
  vertical scale puts the highest point exactly that far above the base, and
  the effective exaggeration is reported back.
- **Sea level.** Samples below 0 m are clamped to 0 by default, so coastlines
  print as a flat plane.
- **Buildings** use the plan scale for their heights, so a building height of
  1x keeps them in true proportion to the map. Raise it for small-scale prints
  where true-proportion buildings are too short to see.
