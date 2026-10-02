import io

import numpy as np
import pytest
from PIL import Image

from satprint.terrain import (
    MAX_TILES,
    BBox,
    ImageryFetcher,
    PrintParams,
    TileFetcher,
    choose_zoom,
    decode_terrarium,
    encode_terrarium,
    fetch_imagery,
    fetch_terrarium,
    gaussian_smooth,
    heightmap_png,
    load_heightmap_file,
    lonlat_to_global_px,
    prepare_relief,
    synthetic_heightmap,
)


def test_terrarium_roundtrip():
    elev = np.linspace(-500, 8000, 256 * 256, dtype=np.float32).reshape(256, 256)
    assert (
        np.abs(decode_terrarium(encode_terrarium(elev)) - elev).max() < 1 / 256 + 1e-3
    )


def test_mercator_origin_and_zoom():
    assert lonlat_to_global_px(-180, 85.0511, 0)[0] == pytest.approx(0)
    assert lonlat_to_global_px(0, 0, 1) == pytest.approx((256, 256))
    bbox = BBox(45.93, 7.58, 46.02, 7.72)
    assert choose_zoom(bbox, 64) < choose_zoom(bbox, 1024) <= 14


def test_bbox_validation():
    with pytest.raises(ValueError):
        BBox(10, 5, 5, 10)
    with pytest.raises(ValueError):
        BBox(88, 0, 89, 1)
    w, h = BBox(0, 0, 0.1, 0.1).ground_size_m()
    assert w == pytest.approx(h, rel=1e-3)


class FakeFetcher(TileFetcher):
    """Serves a smooth analytic surface so the mosaic can be checked."""

    def __init__(self):
        super().__init__(cache_dir="/nonexistent")
        self.calls = []

    def fetch_bytes(self, z, x, y):
        self.calls.append((z, x, y))
        n = 256 * 2**z
        gy, gx = np.mgrid[0:256, 0:256]
        elev = 1000 + (x * 256 + gx) / n * 3000 + (y * 256 + gy) / n * 500
        return encode_terrarium(elev)


class FakeImageryFetcher(ImageryFetcher):
    """Serves JPEG tiles whose red rises eastwards and green southwards
    across ``bbox`` (clipped outside it)."""

    def __init__(self, fail: bool = False, bbox: BBox = BBox(45.9, 7.5, 46.1, 7.9)):
        super().__init__(cache_dir="/nonexistent")
        self.calls = []
        self.fail = fail
        self.bbox = bbox

    def fetch_bytes(self, z, x, y):
        if self.fail:
            raise ConnectionError("imagery server unreachable")
        self.calls.append((z, x, y))
        x0, y0 = lonlat_to_global_px(self.bbox.west, self.bbox.north, z)
        x1, y1 = lonlat_to_global_px(self.bbox.east, self.bbox.south, z)
        gy, gx = np.mgrid[0:256, 0:256]
        rgb = np.zeros((256, 256, 3), np.uint8)
        rgb[..., 0] = np.clip((x * 256 + gx - x0) / (x1 - x0), 0, 1) * 255
        rgb[..., 1] = np.clip((y * 256 + gy - y0) / (y1 - y0), 0, 1) * 255
        buf = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()


def test_fetch_imagery_crop_matches_bbox():
    bbox = BBox(45.9, 7.5, 46.1, 7.9)
    f = FakeImageryFetcher()
    img, meta = fetch_imagery(bbox, max_px=512, fetcher=f)
    assert max(img.size) == 512
    assert meta["tiles"] == len(set(f.calls)) <= MAX_TILES
    rgb = np.asarray(img, dtype=np.float64)
    # row 0 is north (smaller global y), column 0 is west
    assert rgb[:, -1, 0].mean() > rgb[:, 0, 0].mean()
    assert rgb[-1, :, 1].mean() > rgb[0, :, 1].mean()


def test_fetch_imagery_lowers_zoom_to_fit_tile_cap():
    bbox = BBox(40.0, 0.0, 45.0, 10.0)  # wide area at a high pixel target
    f = FakeImageryFetcher()
    _, meta = fetch_imagery(bbox, max_px=8192, fetcher=f)
    assert meta["tiles"] <= MAX_TILES


def test_fetch_terrarium_mosaic_matches_bbox():
    bbox = BBox(45.9, 7.5, 46.1, 7.9)
    f = FakeFetcher()
    hm = fetch_terrarium(bbox, target_cols=200, fetcher=f)
    assert hm.shape[1] == 200
    gw, gh = bbox.ground_size_m()
    assert hm.shape[0] == pytest.approx(200 * gh / gw, abs=1)
    assert len(set(f.calls)) == hm.meta["tiles"]
    # the surface rises eastwards: east column higher than west column
    assert hm.data[:, -1].mean() > hm.data[:, 0].mean()
    # ... and the value matches the analytic function at the bbox center
    z = hm.meta["zoom"]
    px, py = lonlat_to_global_px(bbox.mid_lon, bbox.mid_lat, z)
    expected = 1000 + px / (256 * 2**z) * 3000 + py / (256 * 2**z) * 500
    centre = hm.data[hm.shape[0] // 2, hm.shape[1] // 2]
    assert centre == pytest.approx(expected, abs=2.0)


def test_tile_limit():
    f = FakeFetcher()
    with pytest.raises(ValueError):
        fetch_terrarium(
            BBox(30, 0, 60, 0.5), target_cols=1024, fetcher=f
        )  # tall sliver -> hundreds of tiles
    assert not f.calls


def test_synthetic_is_deterministic():
    a, b = synthetic_heightmap(60, 80, seed=3), synthetic_heightmap(60, 80, seed=3)
    assert np.array_equal(a.data, b.data)
    assert a.max > a.min and a.ground_height_m == pytest.approx(
        a.ground_width_m * 60 / 80
    )


def test_prepare_relief_scaling():
    hm = synthetic_heightmap(60, 80, ground_width_m=8000)
    relief, info = prepare_relief(hm, PrintParams(width_mm=80, exaggeration=2.0))
    # plan scale 80mm/8000m = 0.01 mm/m; vertical = 0.02 mm/m
    assert info["mm_per_m_vertical"] == pytest.approx(0.02)
    assert relief.max() == pytest.approx((hm.max - hm.min) * 0.02)
    assert relief.min() == 0
    relief2, info2 = prepare_relief(hm, PrintParams(width_mm=80, relief_mm=12.5))
    assert relief2.max() == pytest.approx(12.5)
    assert info2["exaggeration"] == pytest.approx(12.5 / (hm.max - hm.min) / 0.01)


def test_sea_level_clamp_and_smoothing():
    hm = synthetic_heightmap(40, 50)
    hm.data[:5] = -300
    relief, info = prepare_relief(hm, PrintParams(clamp_sea_level=True))
    assert info["min_elev_m"] == 0
    relief, info = prepare_relief(hm, PrintParams(clamp_sea_level=False))
    assert info["min_elev_m"] == -300
    s = gaussian_smooth(hm.data, 2.0)
    assert s.shape == hm.data.shape and s.std() < hm.data.std()


def test_load_png_heightmaps():
    arr16 = (np.random.rand(30, 40) * 4000).astype(np.uint16)
    buf = io.BytesIO()
    Image.fromarray(arr16).save(buf, format="PNG")
    hm = load_heightmap_file(buf.getvalue(), "dem.png", ground_width_m=4000)
    assert hm.shape == (30, 40) and hm.max == pytest.approx(arr16.max())
    assert hm.ground_height_m == pytest.approx(3000)
    buf = io.BytesIO()
    Image.fromarray(np.zeros((20, 20), np.uint8)).save(buf, format="PNG")
    hm = load_heightmap_file(buf.getvalue(), "flat.png")
    assert hm.meta["assumed_pixel_m"] == 30.0
    png = heightmap_png(hm)
    assert Image.open(io.BytesIO(png)).size == (20, 20)


def test_mosaic_reports_progress():
    seen = []
    bbox = BBox(45.9, 7.5, 46.1, 7.9)
    hm = fetch_terrarium(
        bbox, target_cols=200, fetcher=FakeFetcher(), progress=lambda *a: seen.append(a)
    )
    n = hm.meta["tiles"]
    assert seen[0] == ("elevation", 0, n) and seen[-1] == ("elevation", n, n)
    assert [d for _, d, _ in seen] == list(range(n + 1))
