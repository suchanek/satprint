# Release Notes -- v0.2.0

> Released: 2026-10-02

Buildings now have real shapes. Domes, onion domes, cones and pyramids mapped
in OpenStreetMap print with their roofs instead of as flat-topped blocks, and a
few landmarks that roof tags cannot describe get exact shapes, starting with
the Sphere in Las Vegas. Overture Maps joins OpenFreeMap and Overpass as a
building source, and a published Docker image runs the whole app with one
command.

## What changed

**Roof shapes.** A building tagged `roof:shape` dome, onion, cone or pyramidal
gets that roof, sized from `roof:height` or `roof:levels`, or from the
footprint when neither is tagged. OpenFreeMap's vector tiles carry no roof
tags, so satprint fetches just the shaped buildings in the area from Overpass
and swaps them in. If that lookup fails, the build finishes with flat roofs and
says so. Lanterns, cupolas and statues mapped on top of a dome stand in a hole
cut in it, so the US Capitol prints with its stepped drum, dome and lantern,
and every solid still only touches its neighbors along shared walls, which
keeps the STL watertight.

**Landmarks.** Some buildings cannot be described by roof tags at all. The
Sphere in Las Vegas used to print as a cylinder; it is now a 157 m sphere cut
off by the ground at 112 m, bulging slightly past its base as the real one
does. The list lives in `satprint/landmarks.py` and matches buildings by their
Wikidata or OSM ID, so it works with every building source.

**Overture Maps.** Overture merges OpenStreetMap with Microsoft and Google
building footprints, so it finds buildings OSM is missing, and it keeps the
roof tags. Pick it as "Building data" in the web app, `--building-source
overture` on the CLI, or `"building_source": "overture"` in the REST API. It
reads only the files Overture's index lists for the area, which takes a few
seconds, and caches the result per Overture release.

**Docker image.** `egsuchanek/satprint` runs on both Intel and ARM machines.
It runs as an unprivileged user, reports its health to Docker, and includes
the Overture source.

## Upgrading

Nothing changes for existing builds except that tagged roofs now have shapes.
To use Overture from a pip or Poetry install, add the new extra:
`pip install "satprint[overture]"`, which brings pyarrow, about 190 MB.

If you ran an earlier Docker image, its cache volume belongs to root and the
new image cannot write to it. Remove it with `docker volume rm satprint-tiles`
and let it be recreated; the cache now mounts at
`/home/satprint/.cache/satprint`.

---

_Full changelog: [CHANGELOG.md](CHANGELOG.md)_
