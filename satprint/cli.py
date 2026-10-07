"""Command-line interface.

Usage:

    satprint serve [--host 0.0.0.0] [--port 7417]
    satprint build --bbox S W N E [--width 120] [--exaggeration 2] -o model.stl
    satprint build --bbox S W N E --glb model.glb -o model.stl
    satprint build --bbox S W N E --buildings [--building-scale 2] -o city.stl
    satprint build --bbox S W N E --buildings --frame 5 --3mf city.3mf -o city.stl
    satprint build --bbox S W N E --bridges [--bridge-piers 20] -o bay.stl
    satprint build --bbox S W N E --buildings --roads [--road-detail major] -o city.stl
    satprint build --synthetic -o demo.stl
    satprint build --file dem.tif --ground-width 15000 -o model.stl
"""

from __future__ import annotations

import argparse
import io
import json
import sys

from .bridges import bridge_mesh, bridges_from_vector_tiles
from .buildings import (
    MAX_BUILDING_AREA_KM2,
    bbox_area_km2,
    building_mesh,
)
from .mesh import (
    check_watertight,
    frame_mesh,
    heightmap_to_mesh,
    merge_building_meshes,
    merge_meshes,
    write_3mf,
    write_binary_stl,
    write_glb,
)
from .osm import (
    OSM_ATTRIBUTION,
    OVERTURE_ATTRIBUTION,
    OverpassClient,
    VectorTileClient,
    fetch_buildings,
)
from .roads import road_mesh, roads_from_vector_tiles
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
from .water import level_water, multicolor_parts, water_from_vector_tiles, water_zoom


def _build(args) -> int:
    """Run ``satprint build``: make the model and write its files."""
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
        try:  # data over water is noisy; levelling refines it, so a failure keeps it
            hm = level_water(hm, VectorTileClient().tiles(bbox, zoom=water_zoom(bbox)))
        except Exception as exc:
            print(f"warning: water not levelled: {exc}", file=sys.stderr)
    else:
        print("error: give --bbox, --file or --synthetic", file=sys.stderr)
        return 2
    if (
        args.glb or args.buildings or args.bridges or args.roads or args.threemf
    ) and hm.bbox is None:
        print(
            "error: --glb, --buildings, --bridges, --roads and --3mf need --bbox",
            file=sys.stderr,
        )
        return 2
    if (
        (args.buildings or args.bridges or args.roads)
        and hm.bbox
        and bbox_area_km2(hm.bbox) > MAX_BUILDING_AREA_KM2
    ):
        print(
            f"error: --buildings, --bridges and --roads are limited to "
            f"{MAX_BUILDING_AREA_KM2:.0f} km2; "
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
    frame = None
    body = mesh
    if args.frame > 0:
        frame_h = args.frame_height or params.base_mm + 1.0
        frame = frame_mesh(info["width_mm"], info["depth_mm"], args.frame, frame_h)
        body = merge_meshes(mesh, frame)
        info["outer_width_mm"] = info["width_mm"] + 2 * args.frame
        info["outer_depth_mm"] = info["depth_mm"] + 2 * args.frame
        info["height_mm"] = max(info["height_mm"], frame_h)
    bmesh = None
    footprints = []  # roads stop at the buildings
    building_credit = (
        OVERTURE_ATTRIBUTION
        if args.buildings and args.building_source == "overture"
        else OSM_ATTRIBUTION
    )
    if args.buildings:
        assert hm.bbox is not None
        print("fetching buildings ...", file=sys.stderr)
        found, warning = fetch_buildings(
            args.building_source, hm.bbox, OverpassClient(), VectorTileClient()
        )
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        footprints = [b.footprint for b in found]
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
    brmesh = None
    if args.bridges:
        assert hm.bbox is not None
        print("fetching bridges ...", file=sys.stderr)
        tiles = VectorTileClient().tiles(hm.bbox, stage="bridges")
        brmesh = bridge_mesh(
            *bridges_from_vector_tiles(tiles),
            water_from_vector_tiles(tiles),
            hm.bbox,
            relief,
            info["width_mm"],
            info["depth_mm"],
            params.base_mm,
            info["mm_per_m_plan"],
            pier_spacing_mm=args.bridge_piers,
        )
        info["bridges"] = brmesh.count
        if brmesh.count:
            info["height_mm"] = max(
                info["height_mm"], float(brmesh.vertices[:, 2].max())
            )
    rmesh = None
    if args.roads:
        assert hm.bbox is not None
        print("fetching roads ...", file=sys.stderr)
        tiles = VectorTileClient().tiles(hm.bbox, stage="roads")
        rmesh = road_mesh(
            roads_from_vector_tiles(tiles),
            water_from_vector_tiles(tiles),
            footprints,
            hm.bbox,
            relief,
            info["width_mm"],
            info["depth_mm"],
            params.base_mm,
            info["mm_per_m_plan"],
            detail=args.road_detail,
            bridges=brmesh,
        )
        info["roads"] = rmesh.count
        if rmesh.count:
            info["height_mm"] = max(
                info["height_mm"], float(rmesh.vertices[:, 2].max())
            )
    # buildings, bridges and roads: everything standing on the terrain
    extras = merge_building_meshes(*(m for m in (bmesh, brmesh, rmesh) if m))
    solid = merge_meshes(body, extras.as_mesh()) if extras.count else body
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
                    body,
                    rows,
                    cols,
                    buf.getvalue(),
                    name=args.name,
                    copyright="; ".join(
                        dict.fromkeys(
                            [IMAGERY_ATTRIBUTION]
                            + ([building_credit] if bmesh and bmesh.count else [])
                            + (
                                [OSM_ATTRIBUTION]
                                if (rmesh and rmesh.count) or (brmesh and brmesh.count)
                                else []
                            )
                        )
                    ),
                    buildings=extras,
                )
            )
        info["texture_meta"] = meta
        print(f"wrote {args.glb}", file=sys.stderr)
    if args.threemf:
        assert hm.bbox is not None
        print("fetching water ...", file=sys.stderr)
        tiles = VectorTileClient().tiles(hm.bbox, zoom=water_zoom(hm.bbox))
        parts, mask = multicolor_parts(
            water_from_vector_tiles(tiles),
            hm.bbox,
            relief,
            info["width_mm"],
            info["depth_mm"],
            params.base_mm,
            sea_level_flat=params.clamp_sea_level and info["min_elev_m"] <= 0,
            buildings=bmesh,
            frame=frame,
            roads=rmesh,
            bridges=brmesh,
        )
        with open(args.threemf, "wb") as fh:
            credits = [OSM_ATTRIBUTION]  # the water
            if bmesh and bmesh.count:
                credits.append(building_credit)
            fh.write(
                write_3mf(
                    parts,
                    name=args.name,
                    attribution="; ".join(dict.fromkeys(credits)),
                )
            )
        info["water_fraction"] = round(float(mask.mean()), 4)
        print(f"wrote {args.threemf}", file=sys.stderr)
    if args.preview:
        with open(args.preview, "wb") as fh:
            fh.write(heightmap_png(hm, params.clamp_sea_level))
    print(json.dumps(info, indent=2, default=str))
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


def _serve(args) -> int:
    """Run ``satprint serve``: the web app under uvicorn."""
    import uvicorn

    uvicorn.run("satprint.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv=None) -> int:
    """Parse ``argv`` and run the chosen subcommand.

    :param argv: arguments without the program name; None reads ``sys.argv``.
    :return: the process exit code.
    """
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
        "--ground-width", type=float, help="real-world width in meters (for --file)"
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
        "--frame",
        type=float,
        default=0.0,
        help="border frame width in mm around the model (0 = none)",
    )
    b.add_argument(
        "--frame-height",
        type=float,
        help="frame height in mm (default: base thickness + 1)",
    )
    b.add_argument(
        "--3mf",
        dest="threemf",
        help="also write a multi-color 3MF here: land, water, buildings, "
        "roads (and bridges) and border as separate parts on four filaments, "
        "the border on the land filament (needs --bbox)",
    )
    b.add_argument(
        "--buildings",
        action="store_true",
        help="add OpenStreetMap buildings (needs --bbox)",
    )
    b.add_argument(
        "--roads",
        action="store_true",
        help="add OpenStreetMap roads as raised strips (needs --bbox)",
    )
    b.add_argument(
        "--road-detail",
        choices=["auto", "major", "all"],
        default="auto",
        help="which roads: auto (by model scale), major (motorway to secondary) or all",
    )
    b.add_argument(
        "--building-scale",
        type=float,
        default=1.0,
        help="building height multiplier; 1 = true proportion",
    )
    b.add_argument(
        "--bridges",
        action="store_true",
        help="add bridges over water from OpenStreetMap (needs --bbox)",
    )
    b.add_argument(
        "--bridge-piers",
        type=float,
        default=20.0,
        metavar="MM",
        help="longest distance between bridge piers in mm; 0 = no piers",
    )
    b.add_argument(
        "--building-source",
        choices=["openfreemap", "overpass", "overture"],
        default="openfreemap",
        help="OpenFreeMap vector tiles (fast, rebuilt weekly), Overpass "
        "(latest OSM edits, slower) or Overture Maps (needs the overture extra)",
    )
    b.add_argument("-o", "--output", default="terrain.stl")
    b.set_defaults(func=_build)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
