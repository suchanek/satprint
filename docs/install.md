# Installation

satprint needs Python 3.12 or 3.13.

## With Poetry

For development, or to build the docs:

```bash
git clone https://github.com/suchanek/satprint.git
cd satprint
poetry install --with dev
```

Add `--extras geotiff` to read the georeferencing of uploaded GeoTIFFs with
`rasterio`, and `--with docs` to build this site.

## With pip

To run the app without the development tooling:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # or: pip install -e ".[geotiff]"
```

## With Docker

A prebuilt image for `linux/amd64` and `linux/arm64` is on Docker Hub:

```bash
docker run -d --name satprint -p 7417:7417 \
  -v satprint-tiles:/home/satprint/.cache/satprint egsuchanek/satprint
```

Then open http://localhost:7417. Tags follow the release version
(`egsuchanek/satprint:0.2.0`), and `latest` is the newest release.

- The volume keeps the downloaded tiles between runs.
- The server runs as the unprivileged user `satprint` (uid 1000). A volume
  created by an older image, which ran as root, is not writable by it; remove
  that volume with `docker volume rm` and let it be recreated.
- `docker ps` reports the container's health from `/api/health`.
- The image has the core install only, without the `geotiff` extra.

To build the image from a clone instead:

```bash
docker build -t satprint .
docker run -d -p 7417:7417 -v satprint-tiles:/home/satprint/.cache/satprint satprint
```

## Check the install

```bash
satprint build --synthetic --resolution 64 -o demo.stl
```

This builds the offline demo terrain and prints a JSON summary ending in
`"watertight": true`.
