"""Command-line interface.

satprint serve [--host 0.0.0.0] [--port 7417]
satprint build --bbox S W N E [--width 120] [--exaggeration 2] -o model.stl
satprint build --bbox S W N E --glb model.glb -o model.stl
satprint build --bbox S W N E --buildings [--building-scale 2] -o city.stl
satprint build --synthetic -o demo.stl
satprint build --file dem.tif --ground-width 15000 -o model.stl
"""

from __future__ import annotations

import argparse
import io
import json
import sys

from .buildings import (
    MAX_BUILDING_AREA_KM2,
    bbox_area_km2,
    building_mesh,
    buildings_from_osm,
    buildings_from_vector_tiles,
)
from .mesh import (
    check_watertight,
    heightmap_to_mesh,
    merge_meshes,
    write_binary_stl,
    write_glb,
)
from .osm import OSM_ATTRIBUTION, OverpassClient, VectorTileClient
from .terrain import (
    IMAGERY_ATTRIBUTION,
    BBox,
    PrintParams,
    fetch_imagery,
    fetch_terrarium,
    heightmap_png,
    load_heightmap_file,
    prepare_relief,
    synthetic_heightmap,
)


def _build(args) -> int:
    if args.synthetic:
        hm = synthetic_heightmap(
            rows=int(args.resolution * 0.75), cols=args.resolution, seed=args.seed
        )
    elif args.file:
        with open(args.file, "rb") as fh:
            hm = load_heightmap_file(
                fh.read(), args.file, ground_width_m=args.ground_width
            )
    elif args.bbox:
        bbox = BBox(*args.bbox)
        print(f"fetching elevation for {bbox} ...", file=sys.stderr)
        hm = fetch_terrarium(bbox, target_cols=args.resolution)
    else:
        print("error: give --bbox, --file or --synthetic", file=sys.stderr)
        return 2
    if (args.glb or args.buildings) and hm.bbox is None:
        print("error: --glb and --buildings need --bbox", file=sys.stderr)
        return 2
    if args.buildings and hm.bbox and bbox_area_km2(hm.bbox) > MAX_BUILDING_AREA_KM2:
        print(
            f"error: --buildings is limited to {MAX_BUILDING_AREA_KM2:.0f} km2; "
            f"this area is {bbox_area_km2(hm.bbox):.0f} km2",
            file=sys.stderr,
        )
        return 2

    params = PrintParams(
        width_mm=args.width,
        base_mm=args.base,
        exaggeration=args.exaggeration,
        relief_mm=args.relief,
        smoothing=args.smoothing,
        clamp_sea_level=not args.keep_bathymetry,
    )
    relief, info = prepare_relief(hm, params)
    mesh = heightmap_to_mesh(relief, info["width_mm"], info["depth_mm"], params.base_mm)
    bmesh = None
    if args.buildings:
        assert hm.bbox is not None
        print("fetching buildings ...", file=sys.stderr)
        if args.building_source == "overpass":
            found = buildings_from_osm(OverpassClient().buildings(hm.bbox))
        else:
            found = buildings_from_vector_tiles(VectorTileClient().tiles(hm.bbox))
        bmesh = building_mesh(
            found,
            hm.bbox,
            relief,
            info["width_mm"],
            info["depth_mm"],
            params.base_mm,
            info["mm_per_m_plan"],
            scale=args.building_scale,
        )
        info["buildings"] = bmesh.count
        if bmesh.count:
            info["height_mm"] = max(
                info["height_mm"], float(bmesh.vertices[:, 2].max())
            )
    solid = merge_meshes(mesh, bmesh.as_mesh()) if bmesh and bmesh.count else mesh
    write_binary_stl(solid, args.output, name=args.name)
    info["triangles"] = solid.triangle_count
    info["volume_cm3"] = solid.volume_mm3() / 1000
    info["watertight"] = check_watertight(solid)["watertight"]
    if args.glb:
        assert hm.bbox is not None
        print("fetching imagery ...", file=sys.stderr)
        img, meta = fetch_imagery(hm.bbox)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        rows, cols = relief.shape
        with open(args.glb, "wb") as fh:
            fh.write(
                write_glb(
                    mesh,
                    rows,
                    cols,
                    buf.getvalue(),
                    name=args.name,
                    copyright=IMAGERY_ATTRIBUTION
                    + ("; " + OSM_ATTRIBUTION if bmesh and bmesh.count else ""),
                    buildings=bmesh,
                )
            )
        info["texture_meta"] = meta
        print(f"wrote {args.glb}", file=sys.stderr)
    if args.preview:
        with open(args.preview, "wb") as fh:
            fh.write(heightmap_png(hm, params.clamp_sea_level))
    print(json.dumps(info, indent=2, default=str))
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


def _serve(args) -> int:
    import uvicorn

    uvicorn.run("satprint.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="satprint",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=7417)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=_serve)

    b = sub.add_parser("build", help="build an STL from the command line")
    src = b.add_mutually_exclusive_group()
    src.add_argument(
        "--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST")
    )
    src.add_argument("--file", help="PNG/TIFF/GeoTIFF heightmap")
    src.add_argument(
        "--synthetic", action="store_true", help="procedural demo terrain (offline)"
    )
    b.add_argument(
        "--ground-width", type=float, help="real-world width in metres (for --file)"
    )
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--resolution", type=int, default=256, help="grid columns (32-1024)")
    b.add_argument("--width", type=float, default=100.0, help="model width in mm")
    b.add_argument("--base", type=float, default=3.0, help="base thickness in mm")
    b.add_argument(
        "--exaggeration", type=float, default=1.5, help="vertical exaggeration"
    )
    b.add_argument(
        "--relief",
        type=float,
        help="fix the relief height in mm (overrides exaggeration)",
    )
    b.add_argument(
        "--smoothing", type=float, default=0.0, help="Gaussian sigma in pixels"
    )
    b.add_argument(
        "--keep-bathymetry",
        action="store_true",
        help="do not clamp below-sea-level data to 0",
    )
    b.add_argument("--name", default="terrain")
    b.add_argument("--preview", help="also write a hillshade PNG here")
    b.add_argument(
        "--glb", help="also write a GLB with satellite imagery here (needs --bbox)"
    )
    b.add_argument(
        "--buildings",
        action="store_true",
        help="add OpenStreetMap buildings (needs --bbox)",
    )
    b.add_argument(
        "--building-scale",
        type=float,
        default=1.0,
        help="building height multiplier; 1 = true proportion",
    )
    b.add_argument(
        "--building-source",
        choices=["openfreemap", "overpass"],
        default="openfreemap",
        help="OpenFreeMap vector tiles (fast, rebuilt weekly) or Overpass "
        "(latest OSM edits, slower)",
    )
    b.add_argument("-o", "--output", default="terrain.stl")
    b.set_defaults(func=_build)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
