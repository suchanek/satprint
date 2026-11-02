# Release Notes -- v0.5.1

> Released: 2026-11-02

satprint 0.5.1 is a security release. It requires Pillow 13.0.0 or later, so a fresh install can no longer resolve Pillow 12.3.0, which carries 41 advisories fixed in 13.0.0, some of them critical. Nothing about the terrain, the meshes or the 3MF output changes.

## What changed

**Pillow 13 is the floor.** satprint reads and writes images through Pillow, and its old range, `pillow>=10`, allowed the vulnerable 12.x line. The floor is now 13.0.0. The lock moves certifi, filelock, virtualenv and opentelemetry-api to patched releases too, which clears every open Dependabot alert.

**CI type-checks the GeoTIFF path.** The type-check job now installs the `geotiff` extra, so it sees `rasterio` the way a full development install does and checks the code that reads GeoTIFF georeferencing.

## Upgrading

Upgrade with `pip install -U satprint`. pip moves Pillow to 13.x if an older version is installed. No settings, presets or saved models change.

---

_Full changelog: [CHANGELOG.md](CHANGELOG.md)_
