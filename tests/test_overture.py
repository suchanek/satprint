from shapely import box, to_wkb

from satprint import overture
from satprint.buildings import DEFAULT_HEIGHT_M, LEVEL_HEIGHT_M, ROOF_PROFILES
from satprint.terrain import BBox


def row(geom, **cols):
    return {"geometry": to_wkb(geom).hex(), **cols}


def test_osm_id_from_sources():
    assert overture._osm_id(
        [{"dataset": "OpenStreetMap", "record_id": "w976405284@3"}]
    ) == ("way/976405284")
    assert overture._osm_id([{"dataset": "OpenStreetMap", "record_id": "r12@1"}]) == (
        "relation/12"
    )
    assert (
        overture._osm_id([{"dataset": "Microsoft ML Buildings", "record_id": "x"}])
        == ""
    )
    assert overture._osm_id(None) == ""


def test_buildings_from_rows():
    rows = {
        "building": [
            row(box(0, 40, 0.001, 40.001), id="a", height=30.0),
            row(box(1, 40, 1.001, 40.001), id="b", num_floors=4),
            row(box(2, 40, 2.001, 40.001), id="c"),
            row(box(3, 40, 3.001, 40.001), id="d", is_underground=True),
            # drawn by its part below
            row(box(4, 40, 4.002, 40.002), id="e", height=99.0, has_parts=True),
            row(
                box(5, 40, 5.001, 40.001),
                id="f",
                height=112.0,
                roof_shape="dome",
                roof_height=None,
                sources=[{"dataset": "OpenStreetMap", "record_id": "w976405284@3"}],
            ),
        ],
        "building_part": [
            row(
                box(4, 40, 4.001, 40.001),
                id="p",
                building_id="e",
                height=50.0,
                roof_shape="onion",
                roof_height=10.0,
            ),
        ],
    }
    found = overture.buildings_from_rows(rows)
    heights = sorted(b.height_m for b in found)
    assert heights == sorted([30.0, 4 * LEVEL_HEIGHT_M, DEFAULT_HEIGHT_M, 50.0, 112.0])
    part = next(b for b in found if b.height_m == 50)
    assert part.is_part and part.profile == ROOF_PROFILES["onion"]
    assert part.roof_height_m == 10.0
    sphere = next(b for b in found if b.height_m == 112)
    assert sphere.osm_id == "way/976405284" and sphere.profile == ROOF_PROFILES["dome"]
    assert 0 < sphere.roof_height_m < 112


class FakeReader:
    def __init__(self, rows):
        self.rows = rows

    def read_all(self):
        return self

    def to_pylist(self):
        return self.rows


class FakeCore:
    def __init__(self):
        self.calls = []

    def get_latest_release(self):
        return "2026-09-23.1"

    def record_batch_reader(self, kind, bbox, release=None, stac=False):
        assert stac  # the full scan needs gigabytes
        self.calls.append((kind, bbox, release))
        geom = to_wkb(box(bbox[0], bbox[1], bbox[0] + 1e-4, bbox[1] + 1e-4))
        if kind == "building_part":
            return FakeReader([])
        return FakeReader([{"id": "a", "height": 20.0, "geometry": geom, "extra": 1}])


def test_fetch_rows_queries_both_types_and_caches(tmp_path, monkeypatch):
    core = FakeCore()
    monkeypatch.setattr(overture, "_client", lambda: core)
    bbox = BBox(36.118, -115.166, 36.1245, -115.158)
    stages = []
    rows = overture.fetch_rows(
        bbox, cache_dir=str(tmp_path), progress=lambda *a: stages.append(a)
    )
    assert [c[0] for c in core.calls] == ["building", "building_part"]
    assert core.calls[0][1] == (-115.166, 36.118, -115.158, 36.1245)
    assert core.calls[0][2] == "2026-09-23.1"
    assert stages[-1] == ("overture buildings", 2, 2)
    assert "extra" not in rows["building"][0]
    assert len(overture.buildings_from_rows(rows)) == 1
    # cached per release: no second query
    assert overture.fetch_rows(bbox, cache_dir=str(tmp_path)) == rows
    assert len(core.calls) == 2
    assert (tmp_path / "2026-09-23.1").is_dir()


def test_no_files_for_the_area_means_no_buildings(tmp_path, monkeypatch):
    core = FakeCore()
    core.record_batch_reader = lambda *a, **k: None
    monkeypatch.setattr(overture, "_client", lambda: core)
    rows = overture.fetch_rows(BBox(0, 0, 0.01, 0.01), cache_dir=str(tmp_path))
    assert rows == {"building": [], "building_part": []}
