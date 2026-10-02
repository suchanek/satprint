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

```bash
docker build -t satprint .
docker run -p 7417:7417 -v satprint-tiles:/root/.cache/satprint satprint
```

The volume keeps the downloaded tiles between runs.

## Check the install

```bash
satprint build --synthetic --resolution 64 -o demo.stl
```

This builds the offline demo terrain and prints a JSON summary ending in
`"watertight": true`.
