"""Command-line interface.

satprint serve [--host 0.0.0.0] [--port 8000]
satprint build --bbox S W N E [--width 120] [--exaggeration 2] -o model.stl
satprint build --synthetic -o demo.stl
satprint build --file dem.tif --ground-width 15000 -o model.stl
"""

from __future__ import annotations

import argparse
import json
import sys

from .mesh import check_watertight, heightmap_to_mesh, write_binary_stl
from .terrain import (
    BBox,
    PrintParams,
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
    write_binary_stl(mesh, args.output, name=args.name)
    info["triangles"] = mesh.triangle_count
    info["volume_cm3"] = mesh.volume_mm3() / 1000
    info["watertight"] = check_watertight(mesh)["watertight"]
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
    s.add_argument("--port", type=int, default=8000)
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
    b.add_argument("-o", "--output", default="terrain.stl")
    b.set_defaults(func=_build)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
