"""
Tests DSM-DIFF (pytest). Synthetisch, ohne Netz, ohne echte Uploads.

GDAL-Tests laufen nur im OSGeo4W/QGIS-Python (sonst per importorskip uebersprungen),
GUI-Tests nur mit tkinter (System-Python). Aufruf siehe README, Abschnitt Tests.
"""

import hashlib
import json
import logging
import os
import struct
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS_DIR = os.path.join(PROJECT_ROOT, "process_scripts")
if PS_DIR not in sys.path:
    sys.path.insert(0, PS_DIR)

import _line_ids                      # noqa: E402
import stac_import                    # noqa: E402
import swisssurface3d as s3d          # noqa: E402

DMC = "Leica DMC-4"
ADS = "Leica ADS100"
L003 = "20260813_0822_41216_003_082221"
L004 = "20260813_0822_41216_004_082750"
L008 = "20260813_0822_41216_008_084617"
L009 = "20260813_0822_41216_009_085104"
FLT_MAX_NEG = -3.4028234663852886e+38

# Am 20.10.2026 an kry-2026-08-13t08222100-dsm-raster.tif gelesen (doppeltes Leerzeichen
# in "Model  - Raster" so im Original)
REAL_DSM_RASTER_DESCRIPTION = (
    "Area: GUPPENFIRN, TerrainModel: Digital Surface Model (DSM photogrammetric "
    "autocorrelation), SourceReferenceSystem: (EPSG:2056) CH1903+ / LV95_LN02, CameraSystem: "
    "Leica DMC-4, Acquisition time: 2026-08-13T08:22:21.00,2026-08-13T08:27:50.00, LineId: "
    "20260813_0822_41216_003_082221,20260813_0822_41216_004_082750, Commentary: Digital "
    "Surface Model  - Raster Mosaic (DSM photogrammetric autocorrelation)")


# ═══ LineIDs, Item-ID, Namen ═════════════════════════════════════════════════
def test_line_ids_wie_gdwh_import():
    assert stac_import.stac_datetime([L004, L003], DMC) == "2026-08-13t08222100"
    assert stac_import.stac_datetime([L009, L008], DMC) == "2026-08-13t08461700"
    assert stac_import.normalized_line_ids([L004, L004, L003], DMC) == [L003, L004]
    with pytest.raises(ValueError):
        stac_import.normalized_line_ids([L003, "20260813_0915_41216_001_091530"], DMC)
    with pytest.raises(ValueError):
        stac_import.normalized_line_ids(["20200821_0952_12504"], DMC)


def test_item_und_asset_name_echter_fall():
    item = stac_import.item_id("kry", [L004, L003], DMC)
    assert item == "kry-2026-08-13t08222100"
    assert stac_import.asset_name(item) == "kry-2026-08-13t08222100-dsm-diff.tif"
    assert stac_import.ASSET_TITLE == "DSM-DIFF"
    assert stac_import.item_id("RAM", [L003], DMC) == "ram-2026-08-13t08222100"
    with pytest.raises(ValueError):
        stac_import.item_id("xyz", [L003], DMC)
    with pytest.raises(ValueError):
        stac_import.item_id("ram", [], DMC)


def test_defaults_und_umgebungen():
    assert stac_import.DEFAULT_ENV == "INT"
    assert stac_import.DEFAULT_AUFTRAGSTYP == "ram"
    assert stac_import.DEFAULT_CAMERA == "Leica DMC-4"
    assert stac_import.STAC_HOSTS == {"INT": "sys-data.int.bgdi.ch", "PROD": "data.geo.admin.ch"}
    item = "kry-2026-08-13t08222100"
    assert stac_import.browser_link(item) == (
        "https://sys-data.int.bgdi.ch/browser/index.html#/collections/"
        "ch.swisstopo.spezialbefliegungen/items/kry-2026-08-13t08222100")
    assert stac_import.browser_link(item, "PROD").startswith("https://data.geo.admin.ch/browser/")
    assert stac_import.asset_url("INT", item, "a.tif") == (
        "https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/ch.swisstopo.spezialbefliegungen"
        "/items/kry-2026-08-13t08222100/assets/a.tif")


# ═══ Beschreibungstext ════════════════════════════════════════════════════════
def test_beschreibung_baut_echtes_dsm_raster_asset_nach():
    desc = stac_import.build_description(
        "GUPPENFIRN", ["Digital Surface Model (DSM photogrammetric autocorrelation)"], DMC,
        [L004, L003], "Digital Surface Model  - Raster Mosaic (DSM photogrammetric autocorrelation)",
        unit=None)
    assert desc == REAL_DSM_RASTER_DESCRIPTION


def test_beschreibung_diff_mit_zwei_terrainmodellen():
    label = stac_import.terrain_model_label
    tm1 = label(stac_import.TERRAIN_MODEL_1_DEFAULT, [2026])
    tm2 = label("swissSURFACE3D", [2023, "2021", 2023])
    assert tm1 == "2026-DSM-photogrammetric"
    assert tm2 == "2021/2023-swissSURFACE3D-DSM"
    desc = stac_import.build_description(
        "GUPPENFIRN", [tm1, label("swissSURFACE3D", [2025])], DMC, [L003, L004],
        stac_import.DEFAULT_COMMENTARY)
    assert desc == (
        "Area: GUPPENFIRN, TerrainModel 1: 2026-DSM-photogrammetric, TerrainModel 2: "
        "2025-swissSURFACE3D-DSM, SourceReferenceSystem: "
        "(EPSG:2056) CH1903+ / LV95_LN02, Unit: metre, CameraSystem: Leica DMC-4, Acquisition time: "
        "2026-08-13T08:22:21.00,2026-08-13T08:27:50.00, LineId: "
        "20260813_0822_41216_003_082221,20260813_0822_41216_004_082750, Commentary: Digital "
        "Surface Model - Difference (TerrainModel 1 minus TerrainModel 2, negative = loss)")
    assert stac_import.area_from_description(desc) == "GUPPENFIRN"
    f = stac_import.description_fields(desc)
    assert f["Unit"] == "metre"
    assert f["SourceReferenceSystem"] == "(EPSG:2056) CH1903+ / LV95_LN02"
    assert label("swissALTI3D", [""]) == "swissALTI3D"
    assert label("swissALTI3D", ["2019"]) == "2019-swissALTI3D"
    assert label("", ["2019"]) == ""
    with pytest.raises(ValueError):
        stac_import.build_description("", ["x"], DMC, [L003], "c")


def test_beschreibung_vertical_accuracy():
    # Spezifikation DSM-Raster 0.5-1 m (1 sigma): sigma_dh = sqrt(2) * sigma, LoD95 = 1.96 * sigma_dh
    assert stac_import.DEFAULT_SIGMA == "0.5-1" and stac_import.DEFAULT_SIGMA_S3D == "0.3"
    assert stac_import.parse_sigma("0.5-1") == (0.5, 1.0)
    assert stac_import.parse_sigma(" 0,5 – 1 ") == (0.5, 1.0)
    assert stac_import.parse_sigma("0.3") == (0.3, 0.3)
    assert stac_import.sigma_text("0,50–1.0") == "0.5-1"
    for bad in ("", "abc", "-0.5", "0", "1-0.5", "0.5-1-2"):
        with pytest.raises(ValueError):
            stac_import.parse_sigma(bad)
    acc = stac_import.vertical_accuracy
    assert acc("0.5-1", "0.5-1") == "0.71-1.41 (1 sigma; LoD95 1.39-2.77)"
    assert acc("0.03", "0.03") == "0.04 (1 sigma; LoD95 0.08)"
    assert acc("0.5-1", "0.1") == "0.51-1.00 (1 sigma; LoD95 1.00-1.97)"
    assert acc("0.5-1", "0.3") == "0.58-1.04 (1 sigma; LoD95 1.14-2.05)"     # swissSURFACE3D
    desc = stac_import.build_description(
        "GUPPENFIRN", ["2026-DSM-photogrammetric", "2025-swissSURFACE3D-DSM"], DMC, [L003],
        "c", accuracy=acc("0.5-1", "0.5-1"))
    assert ", Unit: metre, VerticalAccuracy: 0.71-1.41 (1 sigma; LoD95 1.39-2.77), CameraSystem: " \
        in desc
    f = stac_import.description_fields(desc)
    assert f["VerticalAccuracy"] == "0.71-1.41 (1 sigma; LoD95 1.39-2.77)"
    assert f["Unit"] == "metre" and f["CameraSystem"] == DMC


def test_runner_hoehengenauigkeit_default_je_quelle():
    import _osgeo_runner as runner
    assert runner._accuracy({}, True)[:2] == ("0.5-1", "0.3")
    assert runner._accuracy({}, False)[:2] == ("0.5-1", "0.5-1")
    # Feld vor DIFF-Metadaten vor Default
    assert runner._accuracy({}, True, {"DSMDIFF_SIGMA_OLD": "1"})[1] == "1"
    assert runner._accuracy({"sigma_alt": "0,2"}, True, {"DSMDIFF_SIGMA_OLD": "1"})[1] == "0.2"


def test_beschreibung_terrainmodell_als_stac_link():
    href = ("https://sys-data.int.bgdi.ch/ch.swisstopo.spezialbefliegungen/kry-2026-08-13t08222100/"
            "kry-2026-08-13t08222100-dsm-raster.tif")
    api = ("https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/ch.swisstopo.spezialbefliegungen"
           "/items/kry-2026-08-13t08222100/assets/kry-2026-08-13t08222100-dsm-raster.tif")
    # Daten-href und API-URL: in der Beschreibung steht nur der Asset-Name
    for link in (href, api):
        tm = stac_import.terrain_model_label(stac_import.TERRAIN_MODEL_1_DEFAULT, [2026], link)
        assert tm == "2026-DSM-photogrammetric (kry-2026-08-13t08222100-dsm-raster.tif)"
    desc = stac_import.build_description("GUPPENFIRN", [tm, "2025-DSM-photogrammetric"], DMC,
                                         [L003], "c")
    f = stac_import.description_fields(desc)
    assert f["TerrainModel 1"] == tm and f["TerrainModel 2"] == "2025-DSM-photogrammetric"
    with pytest.raises(ValueError):
        stac_import.terrain_model_label("x", [2026], "https://example.com/a/b/c.tif")


def test_beschreibung_ads_zeiten():
    assert stac_import.acquisition_times(["20200913_1104_12501", "20200913_1054_12501"], ADS) == \
        ["2020-09-13T10:54:00.00", "2020-09-13T11:04:00.00"]


def test_asset_payload():
    p = stac_import.asset_payload("kry-2026-08-13t08222100-dsm-diff.tif", 0.5, "Area: X")
    assert p == {"id": "kry-2026-08-13t08222100-dsm-diff.tif", "title": "DSM-DIFF",
                 "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                 "proj:epsg": 2056, "eo:gsd": 0.5, "description": "Area: X"}


# ═══ Attrappen fuer requests ══════════════════════════════════════════════════
class FakeResponse:
    def __init__(self, status=200, data=None, headers=None, content=b"", url=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self._content = content
        self.url = url

    def json(self):
        if self._data is None:
            raise ValueError("kein JSON")
        return self._data

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Zeichnet alle Aufrufe auf; handler(method, url, kwargs) -> FakeResponse."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _req(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.handler(method, url, kw)
        r.url = url
        return r

    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def put(self, url, **kw):
        return self._req("PUT", url, **kw)

    def post(self, url, **kw):
        return self._req("POST", url, **kw)

    def head(self, url, **kw):
        return self._req("HEAD", url, **kw)

    def delete(self, url, **kw):
        return self._req("DELETE", url, **kw)

    def patch(self, url, **kw):
        return self._req("PATCH", url, **kw)


# ═══ Pruefsumme ═══════════════════════════════════════════════════════════════
def test_multihash_mit_hashlib(tmp_path):
    http = pytest.importorskip("_stac_http")
    f = tmp_path / "abc.bin"
    f.write_bytes(b"abc")
    # sha2-256-Multihash von b"abc": 0x12 0x20 + Digest (identisch zum multihash-Paket)
    expected = "1220ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert http.sha256_multihash(f) == expected
    assert http.same_multihash(expected.upper(), expected)
    assert not http.same_multihash("", expected)


def test_credentials_ohne_inhalt_in_meldungen(tmp_path, monkeypatch):
    http = pytest.importorskip("_stac_http")
    monkeypatch.delenv("STAC_USERNAME", raising=False)
    monkeypatch.delenv("STAC_PASSWORD", raising=False)
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "stac_credentials.json").write_text(json.dumps(
        {"INT": {"username": "testuser", "password": "geheim123", "hostname": "sys-data.int.bgdi.ch"}}),
        encoding="utf-8")
    assert http.load_stac_credentials(sec, "INT") == ("testuser", "geheim123", "sys-data.int.bgdi.ch")
    with pytest.raises(ValueError) as e:
        http.load_stac_credentials(sec, "PROD")
    assert "geheim123" not in str(e.value) and "testuser" not in str(e.value)
    (sec / "stac_credentials.json").write_text('{"INT": {"password": "geheim123",', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        http.load_stac_credentials(sec, "INT")
    assert "geheim123" not in str(e.value)
    monkeypatch.setenv("STAC_USERNAME", "envuser")
    monkeypatch.setenv("STAC_PASSWORD", "envpw")
    assert http.load_stac_credentials(tmp_path / "fehlt", "INT")[:2] == ("envuser", "envpw")


# ═══ swissSURFACE3D: Kachelauswahl, Jahre, Paging ═════════════════════════════
def _s3d_feature(year, e, n, v=5728, checksum="1220AB"):
    item = f"swisssurface3d-raster_{year}_{e}-{n}"
    base = f"https://data.geo.admin.ch/ch.swisstopo.swisssurface3d-raster/{item}/"
    return {"id": item, "assets": {
        f"{item}_0.5_2056_{v}.tif": {"href": base + f"{item}_0.5_2056_{v}.tif",
                                     "proj:epsg": 2056, "checksum:multihash": checksum},
        f"{item}_0.5_2056_{v}.xyz.zip": {"href": base + "x.zip", "proj:epsg": 2056}}}


def test_kacheln_im_extent():
    assert s3d.tiles_for_extent((2600000, 1200000, 2602000, 1201500)) == \
        ["2600-1200", "2601-1200", "2600-1201", "2601-1201"]
    # Extent endet genau auf der Kilometerlinie -> keine Nachbarkachel
    assert s3d.tiles_for_extent((2600250, 1200250, 2601000, 1201000)) == ["2600-1200"]


def test_jahreswahl_mehrere_jahre_fehlende_kachel():
    feats = [_s3d_feature(2019, 2600, 1200), _s3d_feature(2023, 2600, 1200),
             _s3d_feature(2026, 2600, 1200), _s3d_feature(2021, 2601, 1200),
             _s3d_feature(2025, 2600, 1201)]
    avail = s3d.parse_items(feats)
    assert sorted(avail["2600-1200"]) == [2019, 2023, 2026]
    assert avail["2600-1200"][2023]["asset"].endswith("_0.5_2056_5728.tif")   # nicht die .xyz.zip
    needed = s3d.tiles_for_extent((2600000, 1200000, 2602000, 1201500))
    chosen, missing, no_year = s3d.choose_years(avail, needed, 2026)
    assert {k: v["year"] for k, v in chosen.items()} == {"2600-1200": 2023, "2601-1200": 2021,
                                                         "2600-1201": 2025}
    assert missing == ["2601-1201"]
    chosen, _, no_year = s3d.choose_years(avail, needed, 2025)
    assert "2600-1201" in no_year and no_year["2600-1201"] == [2025]
    chosen, _, no_year = s3d.choose_years(avail, needed, 2025, allow_same_year=True)
    assert chosen["2600-1201"]["year"] == 2025 and not no_year
    chosen, _, _ = s3d.choose_years(avail, needed, 2026, allow_same_year=True)
    assert chosen["2600-1200"]["year"] == 2026


def test_suche_paging_ueber_next_links():
    pages = {
        "first": {"features": [_s3d_feature(2021, 2600, 1200)],
                  "links": [{"rel": "next", "href": "https://x/items?cursor=2"}]},
        "https://x/items?cursor=2": {"features": [_s3d_feature(2023, 2600, 1200)],
                                     "links": [{"rel": "next", "href": "https://x/items?cursor=3"}]},
        "https://x/items?cursor=3": {"features": [_s3d_feature(2021, 2601, 1200)], "links": []},
    }

    def handler(method, url, kw):
        return FakeResponse(200, pages["first" if kw.get("params") else url])

    sess = FakeSession(handler)
    feats = s3d.search_items(sess, [8.9, 46.9, 9.0, 47.0])
    assert [f["id"] for f in feats] == ["swisssurface3d-raster_2021_2600-1200",
                                        "swisssurface3d-raster_2023_2600-1200",
                                        "swisssurface3d-raster_2021_2601-1200"]
    assert sess.calls[0][2]["params"]["bbox"] == "8.9,46.9,9.0,47.0"
    assert sess.calls[1][2].get("params") is None      # next-Link traegt bbox/cursor selbst


def test_hoehenbezug_lhn95_im_assetnamen_bricht_ab():
    chosen = {"2600-1200": dict(s3d.parse_items([_s3d_feature(2021, 2600, 1200, v=5729)])
                                ["2600-1200"][2021], year=2021)}
    with pytest.raises(ValueError, match="LHN95"):
        s3d.check_height_reference(chosen)
    ok = {"2600-1200": dict(s3d.parse_items([_s3d_feature(2021, 2600, 1200)])
                            ["2600-1200"][2021], year=2021)}
    s3d.check_height_reference(ok)


# ═══ STAC-Import mit Attrappen: nie ein Item schreiben ════════════════════════
ITEM = "kry-2026-08-13t08222100"
ASSET = ITEM + "-dsm-diff.tif"


def _item_json(area="GUPPENFIRN", gsd=0.5, dt="2026-08-13T08:22:21Z", with_diff=False):
    assets = {f"{ITEM}-dsm-raster.tif": {"title": "DSM-RASTER", "eo:gsd": gsd,
                                         "description": REAL_DSM_RASTER_DESCRIPTION.replace(
                                             "GUPPENFIRN", area)}}
    if with_diff:
        assets[ASSET] = {"title": "DSM-DIFF"}
    return {"id": ITEM, "properties": {"datetime": dt}, "assets": assets}


def _stac_handler(state, item=None, item_status=200, fail_upload=False, item_put_status=201,
                  appears_on_recheck=False):
    """STAC-API-Attrappe. item_status 404: Item fehlt, bis es per PUT angelegt wird.
    appears_on_recheck: beim zweiten GET ist das Item da (z.B. GDWH-Import parallel)."""
    item = item or _item_json()
    state.setdefault("item_gets", 0)

    def current_item():
        if state.get("item_created"):
            has_asset = state.get("payload") and not state.get("deleted")
            return 200, dict(state["item_created"], assets={ASSET: {}} if has_asset else {})
        if item_status == 404:
            if appears_on_recheck and state["item_gets"] >= 2:
                return 200, item
            return 404, {"description": "Not found"}
        return item_status, item

    def handler(method, url, kw):
        if url == stac_import.item_url("INT", ITEM):
            if method == "GET":
                state["item_gets"] += 1
                status, data = current_item()
                return FakeResponse(status, data)
            if method == "PUT":
                state["item_put"] = kw["json"]
                if item_put_status == 201:
                    state["item_created"] = kw["json"]
                return FakeResponse(item_put_status, kw["json"])
            if method == "DELETE":
                state["item_deleted"] = True
                return FakeResponse(200)
        if method == "PUT" and url.endswith(f"/assets/{ASSET}"):
            state["payload"] = kw["json"]
            return FakeResponse(201, {})
        if method == "GET" and url.endswith("/uploads"):
            return FakeResponse(200, {"uploads": []})
        if method == "POST" and url.endswith("/uploads"):
            state["checksum_sent"] = kw["json"]["checksum:multihash"]
            return FakeResponse(201, {"upload_id": "u1", "urls": [{"part": 1, "url": "https://s3/p1"}]})
        if method == "PUT" and url == "https://s3/p1":
            return FakeResponse(500 if fail_upload else 200, headers={"ETag": '"e1"'})
        if method == "POST" and url.endswith("/u1/complete"):
            return FakeResponse(200, {"status": "completed"})
        if method == "POST" and url.endswith("/u1/abort"):
            state["aborted"] = True
            return FakeResponse(200, {"status": "aborted"})
        if method == "GET" and url.endswith(f"/assets/{ASSET}"):
            return FakeResponse(200, {"checksum:multihash": state["checksum_sent"].upper(),
                                      "href": "https://data/x.tif"})
        if method == "HEAD":
            return FakeResponse(200)
        if method == "DELETE" and url.endswith(f"/assets/{ASSET}"):
            state["deleted"] = True
            return FakeResponse(200)
        raise AssertionError(f"unerwarteter Aufruf {method} {url}")
    return handler


def _item_writes(sess):
    """Alle schreibenden Aufrufe auf das Item selbst (nicht auf seine Assets)."""
    item_url = stac_import.item_url("INT", ITEM)
    assert not any(m == "PATCH" for m, _, _ in sess.calls)
    return [(m, u) for m, u, _ in sess.calls if u == item_url and m in ("PUT", "POST", "DELETE")]


def _no_item_writes(sess):
    assert _item_writes(sess) == [], f"Item wurde geschrieben: {_item_writes(sess)}"


RING = [[8.96, 46.96], [8.97, 46.96], [8.97, 46.97], [8.96, 46.97], [8.96, 46.96]]
BOUNDS = (2715800.0, 1202800.0, 2716400.0, 1203300.0)


def _run_import(tmp_path, sess, **kw):
    f = tmp_path / "diff.tif"
    f.write_bytes(b"DIFFDATEN" * 100)
    desc = "Area: GUPPENFIRN, TerrainModel 1: a, TerrainModel 2: b"
    kw.setdefault("bounds_lv95", BOUNDS)
    return f, stac_import.run_stac_import(
        f, "INT", ITEM, desc, 0.5, [L003, L004], DMC, kw.pop("area", "GUPPENFIRN"),
        session=sess, upload_sessions=(sess, sess), auth=("u", "p"), **kw)


@pytest.fixture
def fixed_ring(monkeypatch):
    """LV95 -> WGS84 braucht osr; fuer die Ablauf-Tests genuegt ein fester Ring."""
    monkeypatch.setattr(stac_import, "lv95_bounds_to_wgs84_polygon", lambda b: RING)


def test_stac_import_bestehendes_item_wird_nie_geschrieben(tmp_path):
    pytest.importorskip("requests")
    import _stac_http
    state = {}
    sess = FakeSession(_stac_handler(state))
    f, res = _run_import(tmp_path, sess)
    _no_item_writes(sess)
    assert res["item_created"] is False
    assert state["payload"]["id"] == ASSET and state["payload"]["title"] == "DSM-DIFF"
    assert state["payload"]["eo:gsd"] == 0.5 and state["payload"]["proj:epsg"] == 2056
    assert state["checksum_sent"] == _stac_http.sha256_multihash(f)
    assert res["checksum"] == state["checksum_sent"]
    assert res["browser_link"] == stac_import.browser_link(ITEM, "INT")


def test_stac_import_legt_fehlendes_item_an(tmp_path, fixed_ring):
    """DIFF als erstes Asset: Item fehlt -> genau ein PUT auf das Item, dann das Asset."""
    pytest.importorskip("requests")
    state = {}
    sess = FakeSession(_stac_handler(state, item_status=404))
    f, res = _run_import(tmp_path, sess)
    assert _item_writes(sess) == [("PUT", stac_import.item_url("INT", ITEM))]
    assert state["item_put"] == {"id": ITEM,
                                 "geometry": {"type": "Polygon", "coordinates": [RING]},
                                 "properties": {"datetime": "2026-08-13T08:22:21Z"}}
    first = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in sess.calls[:4]]
    assert first == [("GET", ITEM), ("GET", ITEM), ("PUT", ITEM), ("PUT", ASSET)]
    assert res["item_created"] is True and state["payload"]["id"] == ASSET


def test_stac_import_fehlendes_item_ohne_perimeter(tmp_path, fixed_ring):
    pytest.importorskip("requests")
    sess = FakeSession(_stac_handler({}, item_status=404))
    with pytest.raises(ValueError, match="Perimeter"):
        _run_import(tmp_path, sess, bounds_lv95=None)
    _no_item_writes(sess)


def test_stac_import_item_inzwischen_da_wird_nicht_geschrieben(tmp_path, fixed_ring, caplog):
    pytest.importorskip("requests")
    caplog.set_level(logging.WARNING)
    state = {}
    sess = FakeSession(_stac_handler(state, item_status=404, appears_on_recheck=True))
    _, res = _run_import(tmp_path, sess)
    _no_item_writes(sess)
    assert res["item_created"] is False and "inzwischen" in caplog.text
    assert state["payload"]["id"] == ASSET


def test_stac_import_put_aktualisiert_statt_anlegen_bricht_ab(tmp_path, fixed_ring):
    pytest.importorskip("requests")
    sess = FakeSession(_stac_handler({}, item_status=404, item_put_status=200))
    with pytest.raises(RuntimeError, match="zwischen Pruefung und Anlegen"):
        _run_import(tmp_path, sess)
    assert not any(m == "PUT" and u.endswith(ASSET) for m, u, _ in sess.calls)


def test_stac_import_ohne_dsm_raster_kein_abbruch(tmp_path, caplog):
    pytest.importorskip("requests")
    caplog.set_level(logging.INFO)
    item = {"id": ITEM, "properties": {"datetime": "2026-08-13T08:22:21Z"}, "assets": {}}
    state = {}
    sess = FakeSession(_stac_handler(state, item=item))
    _run_import(tmp_path, sess)
    _no_item_writes(sess)
    assert "noch) kein Asset" in caplog.text and state["payload"]["id"] == ASSET


def test_item_payload_und_datetime():
    assert stac_import.item_datetime([L004, L003], DMC) == "2026-08-13T08:22:21Z"
    p = stac_import.item_payload(ITEM, [L003], DMC, RING)
    assert p == {"id": ITEM, "geometry": {"type": "Polygon", "coordinates": [RING]},
                 "properties": {"datetime": "2026-08-13T08:22:21Z"}}


def test_stac_import_falsche_area_nur_warnung(tmp_path, caplog):
    pytest.importorskip("requests")
    caplog.set_level(logging.WARNING)
    sess = FakeSession(_stac_handler({}, item=_item_json(area="ANDERSWO", gsd=0.25)))
    _run_import(tmp_path, sess)
    text = caplog.text
    assert "Area weicht ab" in text and "GSD weicht ab" in text


def test_stac_import_datetime_und_vorhandenes_asset(tmp_path):
    pytest.importorskip("requests")
    sess = FakeSession(_stac_handler({}, item=_item_json(dt="2026-08-13T08:27:50Z")))
    with pytest.raises(ValueError, match="datetime"):
        _run_import(tmp_path, sess)
    sess = FakeSession(_stac_handler({}, item=_item_json(with_diff=True)))
    with pytest.raises(ValueError, match="existiert in INT bereits"):
        _run_import(tmp_path, sess)
    assert all(c[0] == "GET" for c in sess.calls)
    state = {}
    sess = FakeSession(_stac_handler(state, item=_item_json(with_diff=True)))
    _run_import(tmp_path, sess, replace_existing=True)
    assert state["payload"]["id"] == ASSET


def test_stac_import_fehlgeschlagener_upload_raeumt_neues_asset_weg(tmp_path, monkeypatch):
    pytest.importorskip("requests")
    import _stac_http
    monkeypatch.setattr(_stac_http.time, "sleep", lambda s: None)
    state = {}
    sess = FakeSession(_stac_handler(state, fail_upload=True))
    with pytest.raises(RuntimeError):
        _run_import(tmp_path, sess)
    assert state.get("aborted") and state.get("deleted")
    _no_item_writes(sess)                    # bestehendes Item: weder geschrieben noch geloescht


def test_stac_import_fehlgeschlagener_upload_raeumt_neues_item_weg(tmp_path, monkeypatch,
                                                                   fixed_ring):
    pytest.importorskip("requests")
    import _stac_http
    monkeypatch.setattr(_stac_http.time, "sleep", lambda s: None)
    state = {}
    sess = FakeSession(_stac_handler(state, item_status=404, fail_upload=True))
    with pytest.raises(RuntimeError):
        _run_import(tmp_path, sess)
    assert state.get("aborted") and state.get("deleted") and state.get("item_deleted")
    item_url = stac_import.item_url("INT", ITEM)
    assert _item_writes(sess) == [("PUT", item_url), ("DELETE", item_url)]


def test_lv95_nach_wgs84_polygon():
    pytest.importorskip("osgeo.osr", reason="osr nur im OSGeo4W/QGIS-Python")
    ring = stac_import.lv95_bounds_to_wgs84_polygon((2600000.0, 1199000.0, 2601000.0, 1200000.0))
    assert len(ring) == 5 and ring[0] == ring[-1]
    # Ursprung LV95 (2600000 / 1200000) in WGS84: 7.43863 E / 46.95108 N (swisstopo-
    # Naeherungsformeln). Die Bessel-Werte 7.43958 / 46.95241 gelten auf CH1903+, nicht WGS84.
    assert abs(ring[3][0] - 7.43863) < 1e-4 and abs(ring[3][1] - 46.95108) < 1e-4
    assert ring[1][0] > ring[0][0] and ring[2][1] > ring[1][1]      # Ost bzw. Nord


# ═══ Runner-Logging ═══════════════════════════════════════════════════════════
def test_runner_praefixe():
    import _osgeo_runner as r
    fmt = r._GuiFormatter()
    rec = logging.LogRecord("x", logging.WARNING, "", 0, "zwei\nzeilen", None, None)
    assert fmt.format(rec) == "WARNUNG: zwei\n         zeilen"
    rec = logging.LogRecord("x", logging.ERROR, "", 0, "kaputt", None, None)
    assert fmt.format(rec) == "FEHLER: kaputt"
    rec = logging.LogRecord("x", logging.INFO, "", 0, "info", None, None)
    assert fmt.format(rec) == "info"


# ═══ GDAL: Differenzraster ════════════════════════════════════════════════════
def _gdal():
    return pytest.importorskip("osgeo.gdal", reason="GDAL nur im OSGeo4W/QGIS-Python")


def _dd():
    _gdal()
    import dsm_diff
    return dsm_diff


def write_raster(path, arr, gt, srs="EPSG:2056+5728", nodata=FLT_MAX_NEG, unit=None):
    gdal = _gdal()
    from osgeo import osr
    import numpy as np
    ds = gdal.GetDriverByName("GTiff").Create(str(path), arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32, ["TILED=YES"])
    ds.SetGeoTransform(gt)
    if srs:
        s = osr.SpatialReference()
        s.SetFromUserInput(srs)
        ds.SetSpatialRef(s)
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    if unit is not None:
        b.SetUnitType(unit)
    b.WriteArray(np.asarray(arr, dtype="float32"))
    ds = None
    return str(path)


def read(path):
    gdal = _gdal()
    ds = gdal.Open(str(path))
    a = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return a


def _pair(tmp_path, w=300, h=200, res=0.5, seed=1, srs_neu="EPSG:2056+5728",
          srs_alt="EPSG:2056+5728", name_neu="neu.tif", name_alt="alt.tif"):
    import numpy as np
    rng = np.random.default_rng(seed)
    neu = (1500 + rng.random((h, w)) * 20).astype("float32")
    dh = (rng.random((h, w)) - 0.5).astype("float32")
    alt = (neu - dh).astype("float32")
    gt = (2600000.0, res, 0.0, 1200000.0 + h * res, 0.0, -res)
    pn = write_raster(tmp_path / name_neu, neu, gt, srs_neu)
    pa = write_raster(tmp_path / name_alt, alt, gt, srs_alt)
    return pn, pa, neu, alt, gt


def _run(dd, pn, pa, out, tmp_path, **kw):
    kw.setdefault("staging_dir", str(tmp_path / "staging"))
    return dd.run_dsm_diff(pn, str(out), dsm_alt=pa, alt_quelle="datei", **kw)


def test_diff_gleiches_gitter_exakt(tmp_path):
    dd = _dd()
    import numpy as np
    pn, pa, neu, alt, gt = _pair(tmp_path)
    out = tmp_path / "diff.tif"
    res = _run(dd, pn, pa, out, tmp_path)
    got = read(out)
    expected = (neu.astype("float64") - alt.astype("float64")).astype("float32")
    assert np.array_equal(got, expected)
    assert res["resampling"] == "none"
    assert res["stats"]["valid"] == neu.size
    assert abs(res["stats"]["mean"] - float(expected.astype("float64").mean())) < 1e-9
    assert not (tmp_path / "diff_tmp.tif").exists()
    assert list((tmp_path / "staging").iterdir()) == []          # Staging aufgeraeumt


def test_diff_nodata_loch_und_nan(tmp_path):
    dd = _dd()
    import numpy as np
    pn, pa, neu, alt, gt = _pair(tmp_path, seed=2)
    alt[10:20, 30:40] = FLT_MAX_NEG
    neu[50:55, 5:9] = np.nan
    write_raster(pa, alt, gt)
    write_raster(pn, neu, gt)
    out = tmp_path / "diff.tif"
    _run(dd, pn, pa, out, tmp_path)
    got = read(out)
    nd = np.float32(FLT_MAX_NEG)
    assert (got[10:20, 30:40] == nd).all()
    assert (got[50:55, 5:9] == nd).all()
    assert (got[0:5, 0:5] != nd).all()


def test_diff_halbe_zelle_versetzt_geneigte_ebene(tmp_path, caplog):
    dd = _dd()
    import numpy as np
    caplog.set_level(logging.INFO)

    def plane(x0, y0, w, h, res):
        xs = x0 + (np.arange(w) + 0.5) * res
        ys = y0 - (np.arange(h) + 0.5) * res
        X, Y = np.meshgrid(xs, ys)
        return (1000 + 0.3 * (X - 2600000) + 0.2 * (Y - 1200000)).astype("float64")

    w = h = 80
    neu_gt = (2600000.0, 1.0, 0.0, 1200080.0, 0.0, -1.0)
    alt_gt = (2599999.5, 1.0, 0.0, 1200080.5, 0.0, -1.0)
    pn = write_raster(tmp_path / "neu.tif", plane(neu_gt[0], neu_gt[3], w, h, 1.0), neu_gt)
    pa = write_raster(tmp_path / "alt.tif", plane(alt_gt[0], alt_gt[3], w + 1, h + 1, 1.0), alt_gt)
    out = tmp_path / "diff.tif"
    res = _run(dd, pn, pa, out, tmp_path)
    assert res["resampling"] == "bilinear"
    assert "RESAMPLING" in caplog.text
    got = read(out)
    assert np.abs(got[2:-2, 2:-2]).max() < 2e-3          # Ebene: bilinear exakt bis Float32
    gdal = _gdal()
    ds = gdal.Open(str(out))
    assert ds.GetMetadataItem("DSMDIFF_RESAMPLING").startswith("bilinear")
    ds = None


def test_resampling_methode_je_zellgroesse():
    dd = _dd()
    base = {"res_x": 0.5, "res_y": 0.5, "gt": (2600000.0, 0.5, 0, 1200000.0, 0, -0.5)}
    assert dd.grid_relation(base, dict(base))["method"] == "none"
    shifted = dict(base, gt=(2600000.0 + 0.25, 0.5, 0, 1200000.0, 0, -0.5))
    assert dd.grid_relation(base, shifted)["method"] == "bilinear"
    coarse = dict(base, res_x=2.0, res_y=2.0, gt=(2600000.0, 2.0, 0, 1200000.0, 0, -2.0))
    assert dd.grid_relation(base, coarse)["method"] == "bilinear"
    fine = dict(base, res_x=0.25, res_y=0.25, gt=(2600000.0, 0.25, 0, 1200000.0, 0, -0.25))
    assert dd.grid_relation(base, fine)["method"] == "average"
    # ganzzahliger Versatz im gleichen Gitter -> direkt lesen
    off = dict(base, gt=(2600000.0 - 5.0, 0.5, 0, 1200000.0 + 2.0, 0, -0.5))
    rel = dd.grid_relation(base, off)
    assert (rel["method"], rel["col_off"], rel["row_off"]) == ("none", 10, 4)


def test_diff_kleinere_referenz_rand_nodata(tmp_path, caplog):
    dd = _dd()
    import numpy as np
    caplog.set_level(logging.INFO)
    pn, pa, neu, alt, gt = _pair(tmp_path, seed=3)
    small_gt = (gt[0] + 10 * gt[1], gt[1], 0.0, gt[3] - 20 * gt[1], 0.0, gt[5])
    write_raster(pa, alt[20:180, 10:250], small_gt)
    out = tmp_path / "diff.tif"
    res = _run(dd, pn, pa, out, tmp_path)
    got = read(out)
    nd = np.float32(FLT_MAX_NEG)
    assert (got[:20, :] == nd).all() and (got[:, :10] == nd).all() and (got[:, 250:] == nd).all()
    exp = (neu[20:180, 10:250].astype("float64") - alt[20:180, 10:250]).astype("float32")
    assert np.array_equal(got[20:180, 10:250], exp)
    share = 100.0 * (neu.size - 160 * 240) / neu.size
    assert abs(res["stats"]["no_reference_percent"] - share) < 1e-9
    assert "ohne Referenz" in caplog.text


def test_crs_konflikte(tmp_path, caplog):
    dd = _dd()
    pn, pa, *_ = _pair(tmp_path, srs_alt="EPSG:21781")
    out = tmp_path / "diff.tif"
    with pytest.raises(ValueError, match="LV95"):
        _run(dd, pn, pa, out, tmp_path)
    assert not out.exists() and not (tmp_path / "staging").exists()   # vor dem ersten Schreiben

    pn, pa, *_ = _pair(tmp_path, srs_alt="EPSG:2056+5729")
    with pytest.raises(ValueError, match="Hoehenbezug verschieden"):
        _run(dd, pn, pa, out, tmp_path)
    assert not out.exists()

    caplog.set_level(logging.WARNING)
    pn, pa, *_ = _pair(tmp_path, srs_neu="EPSG:2056", srs_alt="EPSG:2056")
    _run(dd, pn, pa, out, tmp_path)
    assert "NICHT geprueft" in caplog.text


def test_hoehenbezug_aus_dateinamen(tmp_path, caplog):
    dd = _dd()
    caplog.set_level(logging.WARNING)
    pn, pa, *_ = _pair(tmp_path, srs_neu="EPSG:2056", name_neu="2026_X_DSM_50cm_LV95_LN02.tif",
                       srs_alt="EPSG:2056+5728")
    out = tmp_path / "diff.tif"
    _run(dd, pn, pa, out, tmp_path)
    assert "nur aus dem Dateinamen" in caplog.text
    # Name LHN95, Tag LN02 -> Widerspruch -> Abbruch
    pn, pa, *_ = _pair(tmp_path, name_alt="2025_X_DSM_50cm_LV95_LHN95.tif")
    with pytest.raises(ValueError, match="widerspruechlich"):
        _run(dd, pn, pa, tmp_path / "diff2.tif", tmp_path)
    assert dd.height_ref_from_name("swisssurface3d-raster_2021_2600-1200_0.5_2056_5728.tif") == 5728
    assert dd.height_ref_from_name("2026_A_DSM_50cm_LV95_LHN95.tif") == 5729
    assert dd.height_ref_from_name("2026_A_DSM_50cm_LV95.tif") is None


def test_raster_pruefungen(tmp_path):
    dd = _dd()
    import numpy as np
    a = np.ones((10, 10), "float32")
    p = write_raster(tmp_path / "rot.tif", a, (2600000.0, 1.0, 0.1, 1200000.0, 0.0, -1.0))
    with pytest.raises(ValueError, match="rotiert"):
        dd.raster_facts(p, "DSM aktuell")
    p = write_raster(tmp_path / "up.tif", a, (2600000.0, 1.0, 0.0, 1200000.0, 0.0, 1.0))
    with pytest.raises(ValueError, match="north-up"):
        dd.raster_facts(p, "DSM aktuell")
    p = write_raster(tmp_path / "ft.tif", a, (2600000.0, 1.0, 0.0, 1200000.0, 0.0, -1.0), unit="ft")
    with pytest.raises(ValueError, match="Einheit"):
        dd.raster_facts(p, "DSM aktuell")
    p = write_raster(tmp_path / "nocrs.tif", a, (2600000.0, 1.0, 0.0, 1200000.0, 0.0, -1.0), srs=None)
    with pytest.raises(ValueError, match="kein CRS"):
        dd.raster_facts(p, "DSM aktuell")


def test_cog_eigenschaften(tmp_path):
    dd = _dd()
    gdal = _gdal()
    pn, pa, neu, alt, gt = _pair(tmp_path, w=700, h=600, seed=5)
    out = tmp_path / "diff.tif"
    _run(dd, pn, pa, out, tmp_path, metadata={"DSMDIFF_LINE_IDS": f"{L003},{L004}",
                                               "DSMDIFF_STAC_ITEM": ITEM})
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    try:
        ds = gdal.Open(str(out))
        st = ds.GetMetadata("IMAGE_STRUCTURE")
        assert st["LAYOUT"] == "COG" and st["COMPRESSION"] == "DEFLATE" and st["PREDICTOR"] == "3"
        b = ds.GetRasterBand(1)
        assert struct.pack("<f", b.GetNoDataValue()) == struct.pack("<f", FLT_MAX_NEG)
        assert (ds.RasterXSize, ds.RasterYSize) == (700, 600)
        assert tuple(ds.GetGeoTransform()) == gt
        srs = ds.GetSpatialRef()
        assert not srs.IsCompound() and srs.GetAuthorityCode(None) == "2056"
        assert b.GetUnitType() == "metre"
        assert b.GetOverviewCount() >= 1
        md = ds.GetMetadata()
        for key in ("DSMDIFF_DEFINITION", "DSMDIFF_SOURCE_NEW", "DSMDIFF_SOURCE_OLD",
                    "DSMDIFF_HEIGHT_REF_NEW", "DSMDIFF_HEIGHT_REF_OLD", "DSMDIFF_RESAMPLING",
                    "DSMDIFF_CREATED", "TIFFTAG_DATETIME"):
            assert md.get(key), key
        assert md["DSMDIFF_STAC_ITEM"] == ITEM and md["DSMDIFF_SOURCE_NEW"] == "neu.tif"
        assert "negative" in md["DSMDIFF_DEFINITION"]
        assert b.GetMetadataItem("STATISTICS_MEAN") is not None
        assert "dh" in b.GetDescription()
        b = ds = None
    finally:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", None)
    assert not os.path.exists(str(out) + ".aux.xml")


def test_blockgroesse_37_gleich_2048(tmp_path):
    dd = _dd()
    import numpy as np
    pn, pa, neu, alt, gt = _pair(tmp_path, w=150, h=110, seed=6)
    alt[3:9, 4:70] = FLT_MAX_NEG
    write_raster(pa, alt, gt)
    a, b = tmp_path / "a.tif", tmp_path / "b.tif"
    ra = _run(dd, pn, pa, a, tmp_path, block_size=37)
    rb = _run(dd, pn, pa, b, tmp_path, block_size=2048)
    assert np.array_equal(read(a), read(b))
    for k in ("valid", "min", "max"):
        assert ra["stats"][k] == rb["stats"][k]
    assert abs(ra["stats"]["mean"] - rb["stats"]["mean"]) < 1e-9
    assert abs(ra["stats"]["std"] - rb["stats"]["std"]) < 1e-9


def test_ueberschreiben_und_alte_ausgabe_bleibt(tmp_path, monkeypatch):
    dd = _dd()
    pn, pa, *_ = _pair(tmp_path, seed=7)
    out = tmp_path / "diff.tif"
    out.write_bytes(b"ALT")
    with pytest.raises(ValueError, match="existiert bereits"):
        _run(dd, pn, pa, out, tmp_path)
    assert out.read_bytes() == b"ALT"
    monkeypatch.setattr(dd, "check_cog", lambda *a, **k: ["simulierter Fehler"])
    with pytest.raises(RuntimeError, match="simulierter Fehler"):
        _run(dd, pn, pa, out, tmp_path, overwrite=True)
    assert out.read_bytes() == b"ALT"                       # alte Ausgabe unberuehrt
    assert not (tmp_path / "diff_tmp.tif").exists()
    monkeypatch.undo()
    _run(dd, pn, pa, out, tmp_path, overwrite=True)
    assert out.read_bytes() != b"ALT"
    with pytest.raises(ValueError, match="Eingabe"):
        _run(dd, pn, pa, pa, tmp_path, overwrite=True)


def test_abbruch_raeumt_auf(tmp_path):
    dd = _dd()
    pn, pa, *_ = _pair(tmp_path, seed=8)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 5

    out = tmp_path / "diff.tif"
    with pytest.raises(InterruptedError):
        _run(dd, pn, pa, out, tmp_path, block_size=16, cancel=cancel)
    assert not out.exists() and not (tmp_path / "diff_tmp.tif").exists()
    assert list((tmp_path / "staging").iterdir()) == []


# ═══ GDAL: swissSURFACE3D gemockt (Suche, Download, Cache, Mosaik, Diff) ═══════
def _s3d_world(tmp_path, tile_srs="EPSG:2056", lhn95_name=False):
    """Synthetische 1-km-Kacheln (10 m), DSM aktuell ueber 2 x 1.5 km.
    2600-1200: 2019 + 2023, 2601-1200: 2021, 2600-1201: nur 2025, 2601-1201: fehlt."""
    import numpy as np
    remote = tmp_path / "remote"
    remote.mkdir()
    feats, files = [], {}
    v = 5729 if lhn95_name else 5728
    for year, e, n, val in ((2019, 2600, 1200, 990.0), (2023, 2600, 1200, 1000.0),
                            (2021, 2601, 1200, 1100.0), (2025, 2600, 1201, 1200.0)):
        item = f"swisssurface3d-raster_{year}_{e}-{n}"
        name = f"{item}_0.5_2056_{v}.tif"
        a = np.full((100, 100), val, "float32")
        a[40:45, 40:45] = -9999.0
        p = write_raster(remote / name, a, (e * 1000.0, 10.0, 0.0, (n + 1) * 1000.0, 0.0, -10.0),
                         srs=tile_srs, nodata=-9999.0)
        data = open(p, "rb").read()
        href = f"https://data.geo.admin.ch/ch.swisstopo.swisssurface3d-raster/{item}/{name}"
        files[href] = data
        feats.append({"id": item, "assets": {name: {
            "href": href, "proj:epsg": 2056,
            "checksum:multihash": ("1220" + hashlib.sha256(data).hexdigest()).upper()}}})
    counter = {"downloads": 0}

    def handler(method, url, kw):
        if url in files:
            counter["downloads"] += 1
            return FakeResponse(200, content=files[url])
        if kw.get("params"):
            return FakeResponse(200, {"features": feats[:2],
                                      "links": [{"rel": "next", "href": "https://next"}]})
        if url == "https://next":
            return FakeResponse(200, {"features": feats[2:], "links": []})
        raise AssertionError(url)

    neu = np.full((150, 200), 1010.0, "float32")
    pn = write_raster(tmp_path / "2026_TEST_DSM_1000cm_LV95_LN02.tif", neu,
                      (2600000.0, 10.0, 0.0, 1201500.0, 0.0, -10.0))
    return pn, FakeSession(handler), counter


def test_swisssurface3d_end_to_end(tmp_path, caplog):
    dd = _dd()
    gdal = _gdal()
    import numpy as np
    caplog.set_level(logging.INFO)
    pn, sess, counter = _s3d_world(tmp_path)
    out = tmp_path / "out" / "diff.tif"
    out.parent.mkdir()
    mosaic = tmp_path / "out" / "2026_TEST_swissSURFACE3D_50cm_LV95_LN02.tif"
    res = dd.run_dsm_diff(pn, str(out), alt_quelle="swisssurface3d", ref_year=2024,
                          mosaic_out=str(mosaic), session=sess,
                          staging_dir=str(tmp_path / "staging"))
    assert res["s3d"]["tiles"] == {"2600-1200": 2023, "2601-1200": 2021}
    assert res["s3d"]["missing"] == ["2601-1201"]
    assert list(res["s3d"]["no_suitable_year"]) == ["2600-1201"]
    assert "GEMISCHTE JAHRE" in caplog.text and counter["downloads"] == 2

    # Mosaik = Perimeter von DSM aktuell (y 1200000-1201500, 10 m): Zeilen 0-49 liegen in der
    # Kachelreihe N=1201 (keine passende Kachel), Zeilen 50-149 in N=1200
    nd = np.float32(FLT_MAX_NEG)
    m = read(mosaic)
    assert m.shape == (150, 200)
    assert not (m == -9999.0).any()                        # NoData -9999 umgesetzt
    assert (m[50 + 40:50 + 45, 40:45] == nd).all()         # Loch in Kachel 2600-1200
    assert (m[:50, :] == nd).all()                         # obere Reihe: keine passende Kachel
    assert (m[50:, :100] == 1000.0).sum() == 100 * 100 - 25
    assert (m[50:, 100:] == 1100.0).sum() == 100 * 100 - 25
    ds = gdal.Open(str(mosaic))
    assert ds.GetSpatialRef().GetAuthorityCode("VERT_CS") == "5728"
    assert struct.pack("<f", ds.GetRasterBand(1).GetNoDataValue()) == struct.pack("<f", FLT_MAX_NEG)
    assert ds.GetMetadataItem("S3D_MOSAIC_YEARS") == "2021,2023"
    assert ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG"
    ds = None

    d = read(out)
    assert (d[50:, :100][m[50:, :100] != nd] == np.float32(10.0)).all()     # 1010 - 1000
    assert (d[50:, 100:][m[50:, 100:] != nd] == np.float32(-90.0)).all()    # 1010 - 1100
    assert (d[:50, :] == nd).all() and (d[90:95, 40:45] == nd).all()
    assert res["stats"]["valid"] == 2 * (100 * 100 - 25)
    ds = gdal.Open(str(out))
    assert ds.GetMetadataItem("DSMDIFF_SOURCE_OLD_YEARS") == "2021,2023"
    assert ds.GetMetadataItem("DSMDIFF_SOURCE_OLD_TILES") == "2600-1200:2023,2601-1200:2021"
    ds = None

    # zweiter Lauf: Kacheln aus dem Cache, kein neuer Download
    dd.run_dsm_diff(pn, str(out), alt_quelle="swisssurface3d", ref_year=2024,
                    mosaic_out=str(mosaic), session=sess, overwrite=True,
                    staging_dir=str(tmp_path / "staging"))
    assert counter["downloads"] == 2
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["swisssurface3d_cache"]


def test_swisssurface3d_lhn95_bricht_ab(tmp_path):
    dd = _dd()
    pn, sess, counter = _s3d_world(tmp_path, lhn95_name=True)
    out = tmp_path / "diff.tif"
    with pytest.raises(ValueError, match="LHN95"):
        dd.run_dsm_diff(pn, str(out), alt_quelle="swisssurface3d", ref_year=2024,
                        session=sess, staging_dir=str(tmp_path / "staging"))
    assert counter["downloads"] == 0 and not out.exists()


def test_swisssurface3d_kachel_tag_lhn95_bricht_ab(tmp_path):
    dd = _dd()
    pn, sess, counter = _s3d_world(tmp_path, tile_srs="EPSG:2056+5729")
    with pytest.raises(ValueError, match="widerspruechlich"):
        dd.run_dsm_diff(pn, str(tmp_path / "diff.tif"), alt_quelle="swisssurface3d",
                        ref_year=2024, session=sess, staging_dir=str(tmp_path / "staging"))


def test_download_falsche_pruefsumme(tmp_path, monkeypatch):
    pytest.importorskip("requests")
    import _stac_http
    monkeypatch.setattr(_stac_http.time, "sleep", lambda s: None)
    sess = FakeSession(lambda m, u, kw: FakeResponse(200, content=b"daten"))
    chosen = {"2600-1200": {"href": "https://x/k.tif", "checksum": "1220" + "0" * 64, "year": 2021}}
    with pytest.raises(RuntimeError, match="Pruefsumme"):
        s3d.download_tiles(sess, chosen, tmp_path / "cache")
    assert not any((tmp_path / "cache").iterdir())
    assert len(sess.calls) == 3                          # drei Versuche


# ═══ STAC-Link als Eingabe ════════════════════════════════════════════════════
DSM_HREF = (f"https://data.geo.admin.ch/ch.swisstopo.spezialbefliegungen/{ITEM}/"
            f"{ITEM}-dsm-raster.tif")


def test_stac_link_erkennen():
    ref = stac_import.parse_asset_href(DSM_HREF)
    assert (ref["env"], ref["item"], ref["asset"]) == ("PROD", ITEM, f"{ITEM}-dsm-raster.tif")
    api = (f"https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/ch.swisstopo.spezialbefliegungen"
           f"/items/{ITEM}/assets/{ITEM}-dsm-raster.tif")
    assert stac_import.parse_asset_href(api)["env"] == "INT"
    with pytest.raises(ValueError, match="unbekannt"):
        stac_import.parse_asset_href("https://example.com/c/i/a.tif")
    with pytest.raises(ValueError, match="GeoTIFF"):
        stac_import.parse_asset_href(DSM_HREF.replace(".tif", ".copc.laz"))
    assert stac_import.is_url(" https://x ") and not stac_import.is_url(r"D:\a.tif")


def test_beschreibung_zerlegen_und_vorbelegen():
    f = stac_import.description_fields(REAL_DSM_RASTER_DESCRIPTION)
    assert f["Area"] == "GUPPENFIRN"
    assert f["LineId"] == f"{L003},{L004}"
    assert f["Acquisition time"] == "2026-08-13T08:22:21.00,2026-08-13T08:27:50.00"
    assert f["Commentary"].startswith("Digital Surface Model  - Raster")
    s = stac_import.asset_summary(stac_import.parse_asset_href(DSM_HREF),
                                  {"href": DSM_HREF, "eo:gsd": 0.5, "proj:epsg": 2056,
                                   "checksum:multihash": "1220AB",
                                   "description": REAL_DSM_RASTER_DESCRIPTION})
    assert s["auftragstyp"] == "kry" and s["camera"] == "Leica DMC-4" and s["year"] == 2026
    assert s["line_ids"] == [L003, L004] and s["height_ref"] == 5728
    assert s["terrain_model"] == "Digital Surface Model (DSM photogrammetric autocorrelation)"
    assert stac_import.height_ref_from_source_ref_sys("(EPSG:2056) CH1903+ / LV95_LHN95") == 5729
    assert stac_import.height_ref_from_source_ref_sys("") is None


def test_gsd_vergleich_nachgeholt(caplog):
    caplog.set_level(logging.WARNING)
    check = {"ref_gsd": 0.5, "warnings": []}
    stac_import.check_gsd(check, 0.5)
    assert check["warnings"] == []
    stac_import.check_gsd(check, 0.25)
    assert "GSD weicht ab" in caplog.text and len(check["warnings"]) == 1


def _link_session(tmp_path, local_tif, description=REAL_DSM_RASTER_DESCRIPTION):
    """Attrappe: Asset-JSON der STAC-API + die Datei hinter der href."""
    data = open(local_tif, "rb").read()
    checksum = ("1220" + hashlib.sha256(data).hexdigest()).upper()
    api = (f"https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.spezialbefliegungen"
           f"/items/{ITEM}/assets/{ITEM}-dsm-raster.tif")
    counter = {"downloads": 0}

    def handler(method, url, kw):
        if method == "GET" and url == api:
            return FakeResponse(200, {"href": DSM_HREF, "eo:gsd": 0.5, "proj:epsg": 2056,
                                      "checksum:multihash": checksum, "description": description})
        if method == "HEAD" and url == DSM_HREF:
            return FakeResponse(200, headers={"Content-Length": str(len(data))})
        if method == "GET" and url == DSM_HREF:
            counter["downloads"] += 1
            return FakeResponse(200, content=data, headers={"Content-Length": str(len(data))})
        raise AssertionError(f"{method} {url}")
    return FakeSession(handler), counter


def test_diff_mit_stac_link_als_dsm_aktuell(tmp_path, caplog):
    dd = _dd()
    gdal = _gdal()
    import numpy as np
    caplog.set_level(logging.INFO)
    # DSM aktuell wie das echte DSM-RASTER-Asset: CRS-Tag nur EPSG:2056, Name ohne Hoehenbezug;
    # LN02 steht nur in der STAC-Beschreibung
    pn, pa, neu, alt, gt = _pair(tmp_path, seed=11, srs_neu="EPSG:2056",
                                 name_neu=f"{ITEM}-dsm-raster.tif")
    sess, counter = _link_session(tmp_path, pn)
    out = tmp_path / "diff.tif"
    res = dd.run_dsm_diff(DSM_HREF, str(out), dsm_alt=pa, alt_quelle="datei", session=sess,
                          staging_dir=str(tmp_path / "staging"))
    cached = tmp_path / "staging" / "stac_asset_cache" / ITEM / f"{ITEM}-dsm-raster.tif"
    assert cached.exists() and counter["downloads"] == 1
    assert np.array_equal(read(out), (neu.astype("float64") - alt).astype("float32"))
    assert res["height_ref_neu"] == 5728
    assert "NICHT geprueft" not in caplog.text and "STAC-Beschreibung" in caplog.text
    ds = gdal.Open(str(out))
    assert ds.GetMetadataItem("DSMDIFF_SOURCE_NEW_HREF") == DSM_HREF
    assert "STAC description" in ds.GetMetadataItem("DSMDIFF_HEIGHT_REF_NEW")
    ds = None
    # zweiter Lauf: aus dem Cache
    dd.run_dsm_diff(DSM_HREF, str(out), dsm_alt=pa, alt_quelle="datei", session=sess,
                    overwrite=True, staging_dir=str(tmp_path / "staging"))
    assert counter["downloads"] == 1


def test_stac_link_hoehenbezug_widerspruch(tmp_path):
    dd = _dd()
    # Tag sagt LN02, die STAC-Beschreibung LHN95 -> Abbruch
    pn, pa, *_ = _pair(tmp_path, seed=12, name_neu=f"{ITEM}-dsm-raster.tif")
    sess, _ = _link_session(tmp_path, pn, REAL_DSM_RASTER_DESCRIPTION.replace("LV95_LN02", "LV95_LHN95"))
    with pytest.raises(ValueError, match="widerspruechlich"):
        dd.run_dsm_diff(DSM_HREF, str(tmp_path / "diff.tif"), dsm_alt=pa, alt_quelle="datei",
                        session=sess, staging_dir=str(tmp_path / "staging"))
    assert not (tmp_path / "diff.tif").exists()


def _header_session(data, description=REAL_DSM_RASTER_DESCRIPTION):
    """Wie _link_session, aber mit echtem Range-Verhalten (206) und Zaehlung voller Downloads."""
    checksum = ("1220" + hashlib.sha256(data).hexdigest()).upper()
    api = (f"https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.spezialbefliegungen"
           f"/items/{ITEM}/assets/{ITEM}-dsm-raster.tif")
    counter = {"full": 0, "range": 0}

    def handler(method, url, kw):
        if method == "GET" and url == api:
            return FakeResponse(200, {"href": DSM_HREF, "eo:gsd": 0.5, "proj:epsg": 2056,
                                      "checksum:multihash": checksum, "description": description})
        if method == "HEAD":
            return FakeResponse(200, headers={"Content-Length": str(len(data))})
        if method == "GET" and url == DSM_HREF:
            rng = (kw.get("headers") or {}).get("Range")
            if rng:
                counter["range"] += 1
                end = int(rng.split("-")[1])
                return FakeResponse(206, content=data[:end + 1])
            counter["full"] += 1
            return FakeResponse(200, content=data, headers={"Content-Length": str(len(data))})
        raise AssertionError(f"{method} {url}")
    return FakeSession(handler), counter


def test_kopf_reicht_bei_cog(tmp_path):
    """COG: die Ausdehnung kommt aus 1 MB Dateikopf, ohne die ganze Datei."""
    dd = _dd()
    gdal = _gdal()
    import numpy as np
    big = (np.random.default_rng(14).random((1500, 1500)) * 100 + 1500).astype("float32")
    src = write_raster(tmp_path / "src.tif", big, (2600000.0, 0.5, 0.0, 1200750.0, 0.0, -0.5),
                       srs="EPSG:2056")
    cog = tmp_path / "cog.tif"
    gdal.Translate(str(cog), src, options=gdal.TranslateOptions(
        format="COG", creationOptions=["COMPRESS=NONE"]))
    data = cog.read_bytes()
    assert len(data) > 8 * 1048576                     # groesser als beide Kopf-Versuche
    sess, counter = _header_session(data)
    facts, info = dd.remote_header_facts(DSM_HREF, "DSM aktuell", sess, tmp_path / "kopf")
    assert facts is not None and counter == {"full": 0, "range": 1}
    assert facts["bounds"] == (2600000.0, 1200000.0, 2600750.0, 1200750.0)
    assert facts["vertical_stac"] == 5728 and info["area"] == "GUPPENFIRN"
    assert list((tmp_path / "kopf").iterdir()) == []     # Kopfdatei aufgeraeumt


def test_kopf_reicht_nicht_dann_ganze_datei(tmp_path, monkeypatch, capsys):
    """Kein COG (IFD am Dateiende): Kopf reicht nicht -> Runner laedt die ganze Datei."""
    _dd()
    import numpy as np
    import _osgeo_runner as runner
    import _stac_http
    big = (np.random.default_rng(15).random((1600, 1500)) * 100 + 1500).astype("float32")
    p = tmp_path / "gross.tif"
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(str(p), 1500, 1600, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((2600000.0, 0.5, 0.0, 1200800.0, 0.0, -0.5))
    s = osr.SpatialReference()
    s.ImportFromEPSG(2056)
    ds.SetSpatialRef(s)
    ds.GetRasterBand(1).WriteArray(big)
    ds = None
    # Nachtraeglich vergroessertes IFD: libtiff schreibt es ans Dateiende - der Kopf allein
    # reicht dann nicht (so kann ein nachbearbeitetes Nicht-COG aussehen)
    ds = gdal.Open(str(p), gdal.GA_Update)
    ds.SetMetadataItem("NACHTRAG", "x" * 5000)
    ds = None
    data = p.read_bytes()
    assert struct.unpack("<I", data[4:8])[0] > 8 * 1048576       # erstes IFD liegt hinten
    sess, counter = _header_session(data)
    monkeypatch.setattr(_stac_http, "get_session", lambda path=None: sess)
    monkeypatch.setattr(s3d, "search", lambda session, bounds, year: {
        "chosen": {}, "needed": [], "years": [], "missing": [], "no_suitable_year": {},
        "mixed_years": False, "ref_year": year, "bounds": list(bounds)})
    runner._s3d_search({"action": "s3d_search", "dsm_neu": DSM_HREF, "ref_year": 2026,
                        "staging_dir": str(tmp_path / "st")})
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert counter["range"] == 2 and counter["full"] == 1        # 1 MB, 8 MB, dann ganz
    assert res["bounds"] == [2600000.0, 1200000.0, 2600750.0, 1200800.0]


def test_runner_phasen_und_skalierung(capsys):
    import _osgeo_runner as runner
    p = runner._progress_printer(0, 90)
    p(0, "Download a.tif: 1/10 MB")
    p(50, "Differenzrechnung")
    p(100, "COG schreiben")
    up = runner._fraction_progress(runner._progress_printer(90, 100))
    up(0.5, "Upload Teil 1/2")
    out = capsys.readouterr().out.splitlines()
    assert "PHASE:Download a.tif: 1/10 MB" in out and "PHASE:Differenzrechnung" in out
    assert "PROGRESS:0.450000" in out and "PROGRESS:0.900000" in out
    assert "PROGRESS:0.950000" in out and "PHASE:Upload Teil 1/2" in out


def test_runner_info_mit_stac_link(tmp_path, monkeypatch, capsys):
    _dd()
    import _osgeo_runner as runner
    import _stac_http
    pn, *_ = _pair(tmp_path, seed=13)
    sess, counter = _link_session(tmp_path, pn)
    monkeypatch.setattr(_stac_http, "get_session", lambda p=None: sess)
    runner._info({"action": "info", "input_path": DSM_HREF})
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert info["problem"] is None and info["vertical_stac"] == 5728 and info["res_x"] == 0.5
    assert info["stac"]["area"] == "GUPPENFIRN" and info["stac"]["auftragstyp"] == "kry"
    assert counter["downloads"] == 0                     # Info laedt die Datei nicht


# ═══ GDAL: Runner (Subprocess bzw. Aktion direkt) ═════════════════════════════
def test_runner_subprocess_info_und_diff(tmp_path):
    _dd()
    pn, pa, *_ = _pair(tmp_path, srs_neu="EPSG:2056", srs_alt="EPSG:2056", seed=9)
    runner = os.path.join(PS_DIR, "_osgeo_runner.py")
    cfg = tmp_path / "info.json"
    cfg.write_text(json.dumps({"action": "info", "input_path": pn}), encoding="utf-8")
    r = subprocess.run([sys.executable, runner, str(cfg)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout.strip().splitlines()[-1])
    assert info["res_x"] == 0.5 and info["problem"] is None

    out = tmp_path / "diff.tif"
    cfg.write_text(json.dumps({"action": "dsm_diff", "dsm_neu": pn, "dsm_alt": pa,
                               "alt_quelle": "datei", "out_path": str(out),
                               "staging_dir": str(tmp_path / "st")}), encoding="utf-8")
    r = subprocess.run([sys.executable, runner, str(cfg)], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PROGRESS:1.000000" in r.stdout
    assert "WARNUNG: Gleicher Hoehenbezug NICHT geprueft" in r.stdout
    assert "=== Zusammenfassung ===" in r.stdout and out.exists()

    # Zweiter Lauf auf dieselbe Ausgabe: der Runner ersetzt gleichnamige Dateien (Standard)
    before = out.stat().st_mtime_ns
    r = subprocess.run([sys.executable, runner, str(cfg)], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.stat().st_mtime_ns != before

    # Nur mit ausdruecklichem overwrite=False bleibt die Datei stehen (Batch/CLI-Schutz)
    cfg.write_text(json.dumps({"action": "dsm_diff", "dsm_neu": pn, "dsm_alt": pa,
                               "alt_quelle": "datei", "out_path": str(out), "overwrite": False,
                               "staging_dir": str(tmp_path / "st")}), encoding="utf-8")
    r = subprocess.run([sys.executable, runner, str(cfg)], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 1 and "FEHLER: DIFF-COG existiert bereits" in r.stdout


def test_runner_import_scheitert_diff_bleibt(tmp_path, monkeypatch, caplog):
    _dd()
    import _osgeo_runner as runner
    import _stac_http
    pn, pa, *_ = _pair(tmp_path, seed=10)
    out = tmp_path / "diff.tif"
    cfg = {"action": "dsm_diff", "dsm_neu": pn, "dsm_alt": pa, "alt_quelle": "datei",
           "out_path": str(out), "staging_dir": str(tmp_path / "st"),
           "meta": {"auftragstyp": "kry", "camera": DMC, "line_ids": [L003, L004],
                    "area": "GUPPENFIRN", "tm2": "swissALTI3D", "tm2_year": "2019"},
           "stac": {"enabled": True, "env": "INT", "secrets_dir": str(tmp_path)}}
    monkeypatch.setattr(_stac_http, "load_stac_credentials", lambda d, e: ("u", "p", None))
    sess = FakeSession(_stac_handler({}))
    monkeypatch.setattr(_stac_http, "get_session", lambda p=None: sess)

    def boom(*a, **k):
        raise RuntimeError("Upload simuliert fehlgeschlagen")

    monkeypatch.setattr(stac_import, "run_stac_import", boom)
    assert runner._dsm_diff(cfg) == runner.EXIT_IMPORT_FAILED
    assert out.exists()
    _no_item_writes(sess)

    # Item fehlt -> KEIN Abbruch: gerechnet wird, der Import bekommt den Perimeter von
    # DSM aktuell fuer das neue Item
    out.unlink()
    sess404 = FakeSession(_stac_handler({}, item_status=404))
    monkeypatch.setattr(_stac_http, "get_session", lambda p=None: sess404)
    seen = {}

    def record(*a, **k):
        seen.update(k, args=a)
        return {"asset": ASSET, "env": "INT", "browser_link": "x", "item_created": True}

    monkeypatch.setattr(stac_import, "run_stac_import", record)
    assert runner._dsm_diff(cfg) == runner.EXIT_OK
    assert out.exists()
    assert seen["bounds_lv95"] == (2600000.0, 1200000.0, 2600150.0, 1200100.0)
    assert seen["args"][2] == ITEM
    # Beschreibung: lokale Eingaben ohne Pfad, Jahr TerrainModel 1 aus der ersten LineID
    assert ("TerrainModel 1: 2026-DSM-photogrammetric, TerrainModel 2: 2019-swissALTI3D, "
            in seen["args"][3])
    # Ohne Felder gilt die Spezifikation 0.5-1 m je DSM - in Beschreibung und DIFF-Metadaten
    assert "Unit: metre, VerticalAccuracy: 0.71-1.41 (1 sigma; LoD95 1.39-2.77), " in seen["args"][3]
    from osgeo import gdal
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    try:
        ds = gdal.Open(str(out))
        md = ds.GetMetadata()
        ds = None
    finally:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", None)
    assert md["DSMDIFF_SIGMA_NEW"] == "0.5-1" and md["DSMDIFF_SIGMA_OLD"] == "0.5-1"
    assert md["DSMDIFF_VERTICAL_ACCURACY"] == "0.71-1.41 (1 sigma; LoD95 1.39-2.77)"
    _no_item_writes(sess404)                 # die Vorpruefung selbst schreibt nichts

    # Nur STAC-Import: die Felder gewinnen, Abweichung zu den DIFF-Metadaten -> Warnung
    seen.clear()
    imp = {"action": "stac_import", "diff_path": str(out), "stac": cfg["stac"],
           "meta": dict(cfg["meta"], sigma_neu="0.5", sigma_alt="0,5")}
    with caplog.at_level(logging.WARNING):
        assert runner._stac_import(imp) == runner.EXIT_OK
    assert "VerticalAccuracy: 0.71 (1 sigma; LoD95 1.39), " in seen["args"][3]
    assert "weicht von den DIFF-Metadaten ab" in caplog.text
    caplog.clear()
    imp["meta"] = dict(cfg["meta"])          # ohne Felder: Werte aus dem DIFF, keine Warnung
    assert runner._stac_import(imp) == runner.EXIT_OK
    assert "VerticalAccuracy: 0.71-1.41 (1 sigma; LoD95 1.39-2.77), " in seen["args"][3]
    assert "weicht von den DIFF-Metadaten ab" not in caplog.text

    # Ungueltige Hoehengenauigkeit -> Abbruch vor jedem Download und vor der Rechnung
    out.unlink()
    with pytest.raises(ValueError, match="Hoehengenauigkeit"):
        runner._dsm_diff(dict(cfg, meta=dict(cfg["meta"], sigma_alt="x")))
    assert not out.exists()

    # datetime eines vorhandenen Items passt nicht -> Abbruch VOR der Rechnung
    bad =FakeSession(_stac_handler({}, item=_item_json(dt="2026-08-13T09:00:00Z")))
    monkeypatch.setattr(_stac_http, "get_session", lambda p=None: bad)
    with pytest.raises(ValueError, match="datetime"):
        runner._dsm_diff(cfg)
    assert not out.exists()


# ═══ GUI (nur mit tkinter, also im System-Python) ═════════════════════════════
def _gui():
    pytest.importorskip("tkinter", reason="tkinter fehlt (z.B. QGIS-Python)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("gui_dsmdiff",
                                                  os.path.join(PROJECT_ROOT, "GUI_DSMdiff.py"))
    mod = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True        # kein __pycache__ im Hauptverzeichnis
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod


def test_gui_namen_und_jahr():
    g = _gui()
    p = g.parse_dsm_name(r"U:\x\2026_GUPPENFIRN_DSM_50cm_LV95_LN02.tif")
    assert p == {"jahr": "2026", "area": "GUPPENFIRN", "gsd_cm": 50, "href": "LN02"}
    assert g.parse_dsm_name("irgendwas.tif") is None
    assert g.diff_filename("2026", "GUPPENFIRN", 50) == "2026_GUPPENFIRN_DSM-DIFF_50cm_LV95.tif"
    assert g.mosaic_path_for(r"D:\o\a.tif", "2026", "GUPPENFIRN") == \
        os.path.join(r"D:\o", "2026_GUPPENFIRN_swissSURFACE3D_50cm_LV95_LN02.tif")
    assert g.acquisition_year([L004], DMC, "2020") == (2026, "LineIDs")
    assert g.acquisition_year([], DMC, "2025") == (2025, "Feld Jahr")
    assert g.acquisition_year([], DMC, "") == (None, None)
    assert g.normalize_tif_path('"D:\\o\\a"') == "D:\\o\\a.tif"


def test_gui_vorbelegung_vorschau_und_defaults(tmp_path):
    g = _gui()
    app = g.DSMDiffApp()
    try:
        assert app.env_var.get() == "INT"
        assert app._auftragstyp() == "ram"
        assert app.camera_var.get() == "Leica DMC-4"
        assert app.alt_mode_var.get() == g.ALT_S3D
        assert app.stac_var.get() is False and app.replace_var.get() is False
        neu = tmp_path / "2026_GUPPENFIRN_DSM_50cm_LV95_LN02.tif"
        app.neu_var.set(str(neu))
        app._on_neu_changed()
        assert app.jahr_var.get() == "2026" and app.area_var.get() == "GUPPENFIRN"
        assert app.out_var.get() == str(tmp_path / "2026_GUPPENFIRN_DSM-DIFF_50cm_LV95.tif")
        app.auftragstyp_var.set(next(l for l in g.AUFTRAGSTYP_LABELS if l.startswith("kry ")))
        added, skipped = app.lineid_w.add_ids([L004, L003, "kaputt"])
        assert added == [L004, L003] and len(skipped) == 1
        app._refresh_preview()
        txt = {k: v.cget("text") for k, v in app._preview.items()}
        assert txt["item"] == "kry-2026-08-13t08222100"
        assert txt["asset"] == "kry-2026-08-13t08222100-dsm-diff.tif"
        assert txt["title"] == "DSM-DIFF"
        assert txt["link"].startswith("https://sys-data.int.bgdi.ch/")
        assert "TerrainModel 1: 2026-DSM-photogrammetric, TerrainModel 2: <Jahr nach" in txt["desc"]
        assert "-swissSURFACE3D-DSM, " in txt["desc"]
        # Hoehengenauigkeit: Default je Quelle DSM alt, geht in Vorschau und Config
        assert app.sigma_neu_var.get() == "0.5-1" and app.sigma_alt_var.get() == "0.3"
        assert "VerticalAccuracy: 0.58-1.04 (1 sigma; LoD95 1.14-2.05), " in txt["desc"]
        app.alt_mode_var.set(g.ALT_FILE)
        app._on_alt_change()
        assert app.sigma_alt_var.get() == "0.5-1"
        app.sigma_alt_var.set("0.2")             # von Hand geaendert: bleibt beim Umschalten
        app.alt_mode_var.set(g.ALT_S3D)
        app._on_alt_change()
        assert app.sigma_alt_var.get() == "0.2"
        app._refresh_preview()
        assert "VerticalAccuracy: 0.54-1.02 (1 sigma; LoD95 1.06-2.00), " in \
            app._preview["desc"].cget("text")
        assert app._build_cfg()["meta"]["sigma_alt"] == "0.2"
        app.sigma_alt_var.set("x")
        app._refresh_preview()
        assert app._preview["desc"].cget("text").startswith("(Hoehengenauigkeit 'x'")
        assert any(e.startswith("DSM alt: Hoehengenauigkeit") for e in app._validate())
        app.sigma_alt_var.set(g.stac_import.DEFAULT_SIGMA)
        app.env_var.set("PROD")
        app._refresh_preview()
        assert app._preview["link"].cget("text").startswith("https://data.geo.admin.ch/")
        cfg = app._build_cfg()
        assert cfg["stac"]["env"] == "PROD" and cfg["alt_quelle"] == g.ALT_S3D
        assert cfg["ref_year"] == 2026 and cfg["meta"]["line_ids"] == [L003, L004]
        assert cfg["mosaic_out"].endswith("2026_GUPPENFIRN_swissSURFACE3D_50cm_LV95_LN02.tif")
        app.alt_mode_var.set(g.ALT_FILE)
        app._on_alt_change()
        app.tm2_var.set("swissALTI3D")
        app.tm2_year_var.set("2019")
        app._refresh_preview()
        assert "TerrainModel 2: 2019-swissALTI3D, " in app._preview["desc"].cget("text")
        # STAC-Links: der Asset-Name steht in Klammern, ein lokaler Pfad nie
        app.neu_var.set(DSM_HREF)
        app.alt_var.set(DSM_HREF.replace("2026-08-13t08222100", "2025-08-20t10000000"))
        app.tm2_var.set(g.stac_import.TERRAIN_MODEL_1_DEFAULT)
        app.tm2_year_var.set("2025")
        app._refresh_preview()
        desc = app._preview["desc"].cget("text")
        assert (f"TerrainModel 1: 2026-DSM-photogrammetric ({ITEM}-dsm-raster.tif), "
                "TerrainModel 2: 2025-DSM-photogrammetric "
                "(kry-2025-08-20t10000000-dsm-raster.tif), ") in desc
        app.tm2_year_var.set("")
        app.stac_var.set(True)
        assert any("TerrainModel 2: Jahr" in e for e in app._validate())
    finally:
        app.destroy()


def test_gui_validierung_nur_formales(tmp_path):
    g = _gui()
    app = g.DSMDiffApp()
    try:
        errors = app._validate()
        assert any("DSM aktuell fehlt" in e for e in errors)
        neu = tmp_path / "neu.tif"
        neu.write_bytes(b"x")
        app.neu_var.set(str(neu))
        app.out_var.set(str(neu))
        app.jahr_var.set("2026")
        errors = app._validate()
        assert any("keine Eingabe" in e or "existiert bereits" in e for e in errors)
        app.out_var.set(str(tmp_path / "diff.tif"))
        app.stac_var.set(True)
        app.secrets_var.set(str(tmp_path / "gibtsnicht"))
        errors = app._validate()
        assert any("LineIDs fehlen" in e for e in errors)
        assert any("Secrets-Ordner nicht gefunden" in e for e in errors)
        app._update_start_btn_state()
        assert str(app.start_btn.cget("state")) == "disabled"
        # 'Nur STAC-Import' nimmt das eigene Feld, nicht die Ausgabe
        app.out_var.set(str(neu))
        errors = app._validate(import_only=True)
        assert any("Vorhandenes DIFF-COG" in e for e in errors)
        app.import_diff_var.set(str(neu))
        assert not any("Vorhandenes DIFF-COG" in e for e in app._validate(import_only=True))
        # STAC-Link als Eingabe: nur die Form wird geprueft
        app.neu_var.set("https://example.com/x/y/z.tif")
        assert any("unbekannt" in e for e in app._validate())
        app.neu_var.set(DSM_HREF)
        assert not any("DSM aktuell" in e for e in app._validate())
    finally:
        app.destroy()


def test_gui_vorbelegung_aus_stac_und_aus_diff():
    g = _gui()
    app = g.DSMDiffApp()
    try:
        summary = stac_import.asset_summary(
            stac_import.parse_asset_href(DSM_HREF),
            {"href": DSM_HREF, "eo:gsd": 0.5, "description": REAL_DSM_RASTER_DESCRIPTION})
        app.camera_var.set("Leica ADS100")
        assert summary["auftragstyp"] == "kry"
        app._apply_stac_neu(summary)
        # Auftragstyp bleibt 'ram', auch wenn DSM aktuell aus einem kry-Item stammt
        assert app._auftragstyp() == "ram" and app.area_var.get() == "GUPPENFIRN"
        assert app.camera_var.get() == "Leica DMC-4" and app.jahr_var.get() == "2026"
        assert app.lineid_w.get_ids() == [L003, L004]
        assert app._suggested_out_name() == "2026_GUPPENFIRN_DSM-DIFF_50cm_LV95.tif"
        app._apply_stac_alt(dict(summary, terrain_model="swissALTI3D", year=2019))
        assert app.tm2_var.get() == "swissALTI3D" and app.tm2_year_var.get() == "2019"

        app.lineid_w.clear()
        app.area_var.set("")
        app.auftragstyp_var.set(g.DEFAULT_AUFTRAGSTYP_LABEL)
        app._apply_diff_metadata({"DSMDIFF_DEFINITION": "dh", "DSMDIFF_STAC_ITEM": ITEM,
                                  "DSMDIFF_AREA": "GUPPENFIRN", "DSMDIFF_CAMERA": DMC,
                                  "DSMDIFF_LINE_IDS": f"{L003},{L004}",
                                  "DSMDIFF_SOURCE_OLD_TYPE": "swissSURFACE3D",
                                  "DSMDIFF_SIGMA_NEW": "0.5", "DSMDIFF_SIGMA_OLD": "0.3-0.8"})
        assert app._auftragstyp() == "kry" and app.area_var.get() == "GUPPENFIRN"
        assert app.lineid_w.get_ids() == [L003, L004]
        assert app.sigma_neu_var.get() == "0.5" and app.sigma_alt_var.get() == "0.3-0.8"
        # DIFF mit eigenem DSM alt: der Wert aus dem DIFF gewinnt gegen den Default je Quelle
        app.sigma_alt_var.set(g.stac_import.DEFAULT_SIGMA_S3D)
        app._apply_diff_metadata({"DSMDIFF_DEFINITION": "dh", "DSMDIFF_SOURCE_OLD_TYPE": "file",
                                  "DSMDIFF_SIGMA_NEW": "0.5-1", "DSMDIFF_SIGMA_OLD": "0.3"})
        assert app.alt_mode_var.get() == g.ALT_FILE and app.sigma_alt_var.get() == "0.3"
        app._apply_diff_metadata({})
        assert "keine DSMDIFF-Metadaten" in app.import_diff_problem_lbl.cget("text")
    finally:
        app.destroy()


def test_gui_kacheln_button():
    g = _gui()
    app = g.DSMDiffApp()
    try:
        btn, s = app.s3d_search_btn, g.ttk.Style(app)
        app.neu_var.set(DSM_HREF)
        app.jahr_var.set("2026")
        app._refresh_preview()
        assert btn.cget("text") == "Kacheln suchen" and str(btn.cget("style")) == "Amber.TButton"
        assert not btn.instate(["disabled"])
        assert s.lookup("Amber.TButton", "foreground") == g.DARK["hint"]
        assert s.lookup("Amber.TButton", "foreground", ["disabled"]) == g.DARK["fg_dim"]
        app._s3d_result, app._s3d_key = {"years": [2024]}, app._s3d_key_now()
        app._preview_sig = None
        app._refresh_preview()
        assert btn.cget("text") == "Kacheln geladen" and str(btn.cget("style")) == "Ok.TButton"
        assert s.lookup("Ok.TButton", "foreground") == g.DARK["ok"]
        app.alt_mode_var.set(g.ALT_FILE)
        app._on_alt_change()
        assert btn.instate(["disabled"])
        app._enable_s3d_btn()                 # Suche endet, waehrend Eigenes DSM gewaehlt ist
        assert btn.instate(["disabled"])
        app.alt_mode_var.set(g.ALT_S3D)
        app._on_alt_change()
        assert not btn.instate(["disabled"]) and btn.cget("text") == "Kacheln geladen"
        app.jahr_var.set("2025")              # andere Eingabe -> Ergebnis verfaellt
        app._refresh_preview()
        assert app._s3d_result is None and btn.cget("text") == "Kacheln suchen"
        app._apply_theme(False)
        assert s.lookup("Amber.TButton", "foreground") == g.LIGHT["hint"]
    finally:
        app.destroy()


def test_gui_sanduhr_und_phase():
    g = _gui()
    app = g.DSMDiffApp()
    try:
        app._set_busy(True)
        app._set_busy(True)
        app._set_busy(False)
        assert str(app.cget("cursor")) == "watch"           # ein Aufruf laeuft noch
        app._set_busy(False)
        assert str(app.cget("cursor")) == ""
        app._phase, app._phase_group, app._last_frac, app._progress_start = "", None, 0.0, None
        app._update_progress(0.05)
        app._set_phase("Download kry-…-dsm-raster.tif: 30/594 MB")
        txt = app._progress_lbl.cget("text")
        assert "30/594 MB" in txt and "verbleibend" not in txt
        app._set_phase("Differenzrechnung")
        app._update_progress(0.5)
        txt = app._progress_lbl.cget("text")
        assert "Differenzrechnung" in txt and "verbleibend" in txt
    finally:
        app.destroy()
