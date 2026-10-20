"""
swisssurface3d.py - swissSURFACE3D Raster aus dem swisstopo-STAC als Referenz ("DSM alt").

Ablauf:
  1. Bounds von DSM aktuell (LV95) -> WGS84-BBox (osr) -> items?bbox=... mit Paging ueber
     die 'next'-Links
  2. Kacheln ueber <E>-<N> im Item-Namen exakt gegen den LV95-Extent filtern
     (Kachelname = linke untere Ecke in km, 1 km x 1 km)
  3. Jahr je Kachel: das juengste Jahr VOR dem Aufnahmejahr von DSM aktuell (feste Regel;
     beim gleichen Jahr ist unklar, ob die Lidar-Befliegung vorher oder nachher war - die
     Items tragen nur YYYY-01-01). Kachel ohne passendes Jahr -> wie fehlend (NoData, Warnung)
  4. COG-Download in einen Cache im Staging (bleibt ueber Laeufe bestehen), Pruefsumme
     gegen checksum:multihash, gueltige Dateien werden nicht neu geladen
  5. VRT ueber die Kacheln, ohne Resampling, NoData der Kacheln (-9999) -> -FLT_MAX.
     Daraus macht dsm_diff das Mosaik-COG (Merged TIFF), die Basis fuer das DIFF.

Am 20.10.2026 an der oeffentlichen API nachgeprueft: Asset ..._0.5_2056_5728.tif (COG,
0.5 m, Float32, NoData -9999). Der CRS-Tag der Kacheln ist nur EPSG:2056. Auch Collection
(summaries proj:epsg [2056]), Items und Assets (proj:epsg 2056) nennen keinen Hoehenbezug.
Die einzige Angabe dazu im STAC ist der Assetname <gsd>_<EPSG horizontal>_<EPSG vertikal>:
_2056_5728 = LV95 + LN02. Deshalb (check_height_reference):
  - bei ALLEN gewaehlten Kacheln muss das Asset _2056_5728 heissen und proj:epsg 2056 tragen
  - bei einer heruntergeladenen Kachel wird zusaetzlich der CRS-Tag geprueft (dsm_diff):
    traegt sie einen Hoehenbezug, muss es 5728 sein
LHN95 (5729) oder ein anderer Hoehenbezug -> Abbruch vor dem Download.
"""

import logging
import math
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

S3D_API = "https://data.geo.admin.ch/api/stac/v0.9"
S3D_COLLECTION = "ch.swisstopo.swisssurface3d-raster"
S3D_ITEM_PATTERN = re.compile(r"^swisssurface3d-raster_(\d{4})_(\d{4})-(\d{4})$")
# Assetname <item>_<gsd>_<EPSG horizontal>_<EPSG vertikal>.tif, z.B. ..._0.5_2056_5728.tif
S3D_ASSET_PATTERN = re.compile(r"^(?P<item>.+)_(?P<gsd>\d+(?:\.\d+)?)_(?P<h>\d{4})_(?P<v>\d{4})\.tif$")
S3D_ASSET_GSD = "0.5"
S3D_HORIZONTAL_EPSG = 2056
S3D_TILE_SIZE_M = 1000.0
S3D_GSD_M = 0.5
S3D_VERTICAL_EPSG = 5728          # LN02, laut Produkt (Assetname _2056_5728)
S3D_SRS = "EPSG:2056+5728"        # CRS-Tag des Mosaiks
CACHE_DIRNAME = "swisssurface3d_cache"
PAGE_LIMIT = 100
MAX_PAGES = 2000                  # Schutz gegen endloses Paging


def _cancelled(cancel):
    if cancel is None:
        return False
    return cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel())


def tile_key(e_km, n_km):
    return f"{int(e_km):04d}-{int(n_km):04d}"


def tiles_for_extent(bounds):
    """Alle 1-km-Kacheln (Schluessel 'EEEE-NNNN'), die den LV95-Extent (minx, miny,
    maxx, maxy) schneiden. Ein Extent, der genau auf einer Kilometerlinie endet,
    zieht die Nachbarkachel nicht mit."""
    minx, miny, maxx, maxy = bounds
    eps = 1e-6
    e0 = math.floor((minx + eps) / S3D_TILE_SIZE_M)
    e1 = math.ceil((maxx - eps) / S3D_TILE_SIZE_M) - 1
    n0 = math.floor((miny + eps) / S3D_TILE_SIZE_M)
    n1 = math.ceil((maxy - eps) / S3D_TILE_SIZE_M) - 1
    return [tile_key(e, n) for n in range(n0, n1 + 1) for e in range(e0, e1 + 1)]


def extent_to_wgs84_bbox(bounds, margin_deg=0.001):
    """LV95-Extent -> [lon_min, lat_min, lon_max, lat_max] fuer die STAC-Suche. Die
    Kanten werden verdichtet (die Abbildung ist nicht linear), dazu ein kleiner Rand -
    zu viel gefundene Kacheln filtert tiles_for_extent ohnehin wieder heraus."""
    from osgeo import osr
    osr.UseExceptions()
    src = osr.SpatialReference()
    src.ImportFromEPSG(2056)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    minx, miny, maxx, maxy = bounds
    pts = []
    for i in range(11):
        f = i / 10.0
        x = minx + f * (maxx - minx)
        y = miny + f * (maxy - miny)
        pts += [(x, miny), (x, maxy), (minx, y), (maxx, y)]
    lonlat = [tr.TransformPoint(x, y)[:2] for x, y in pts]
    lons = [p[0] for p in lonlat]
    lats = [p[1] for p in lonlat]
    return [round(min(lons) - margin_deg, 6), round(min(lats) - margin_deg, 6),
            round(max(lons) + margin_deg, 6), round(max(lats) + margin_deg, 6)]


def search_items(session, bbox, cancel=None):
    """Alle Items der Collection in der BBox, Paging ueber den 'next'-Link."""
    url = f"{S3D_API}/collections/{S3D_COLLECTION}/items"
    params = {"bbox": ",".join(str(v) for v in bbox), "limit": PAGE_LIMIT}
    features = []
    for page in range(1, MAX_PAGES + 1):
        if _cancelled(cancel):
            raise InterruptedError("Abbruch durch den Benutzer (Kachelsuche)")
        r = session.get(url, params=params)
        if r.status_code != 200:
            raise RuntimeError(f"swissSURFACE3D-Suche fehlgeschlagen: HTTP {r.status_code} "
                               f"fuer {url}")
        data = r.json()
        features += data.get("features", [])
        nxt = next((l.get("href") for l in data.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            return features
        url, params = nxt, None      # der next-Link traegt bbox/cursor/limit bereits
    raise RuntimeError(f"swissSURFACE3D-Suche: mehr als {MAX_PAGES} Seiten - abgebrochen.")


def parse_items(features):
    """{'EEEE-NNNN': {jahr: {'item', 'asset', 'href', 'checksum', 'epsg_h', 'epsg_v',
    'proj_epsg'}}} aus den STAC-Items. Genommen wird das 0.5-m-GeoTIFF-Asset (nicht die
    .xyz.zip); Items ohne ein solches Asset werden mit Warnung uebergangen."""
    tiles = {}
    for f in features:
        m = S3D_ITEM_PATTERN.match(f.get("id", ""))
        if not m:
            continue
        year, e_km, n_km = int(m.group(1)), m.group(2), m.group(3)
        found = None
        for key, asset in (f.get("assets") or {}).items():
            am = S3D_ASSET_PATTERN.match(key)
            if am and am.group("item") == f["id"] and am.group("gsd") == S3D_ASSET_GSD \
                    and asset.get("href"):
                found = (key, asset, am)
                break
        if found is None:
            logger.warning(f"swissSURFACE3D-Item {f['id']} ohne {S3D_ASSET_GSD}-m-GeoTIFF-Asset "
                           f"- uebergangen.")
            continue
        key, asset, am = found
        tiles.setdefault(tile_key(e_km, n_km), {})[year] = {
            "item": f["id"], "asset": key, "href": asset["href"],
            "checksum": asset.get("checksum:multihash", ""),
            "epsg_h": int(am.group("h")), "epsg_v": int(am.group("v")),
            "proj_epsg": asset.get("proj:epsg")}
    return tiles


def check_height_reference(chosen):
    """Hoehenbezug der gewaehlten Kacheln laut STAC (Assetname): muss LN02 (5728) sein,
    horizontal LV95 (Name und proj:epsg). ValueError sonst - vor jedem Download."""
    bad_v = {k: v["epsg_v"] for k, v in chosen.items() if v.get("epsg_v") != S3D_VERTICAL_EPSG}
    if bad_v:
        names = {5729: "LHN95"}
        raise ValueError(
            "swissSURFACE3D-Kacheln mit anderem Hoehenbezug als LN02 (EPSG:5728) laut STAC-"
            "Assetname - Abbruch: " + ", ".join(
                f"{k} ({names.get(v, 'EPSG')}:{v})" for k, v in sorted(bad_v.items())))
    bad_h = [k for k, v in chosen.items()
             if v.get("epsg_h") != S3D_HORIZONTAL_EPSG
             or v.get("proj_epsg") not in (None, S3D_HORIZONTAL_EPSG)]
    if bad_h:
        raise ValueError("swissSURFACE3D-Kacheln nicht in LV95 (EPSG:2056) laut STAC: "
                         + ", ".join(sorted(bad_h)))
    if chosen:
        logger.info(f"  Hoehenbezug laut STAC: alle {len(chosen)} Kachel(n) _2056_5728 "
                    f"(LV95 + LN02), proj:epsg 2056")


def choose_years(available, needed, ref_year, allow_same_year=False):
    """Jahr je benoetigter Kachel: das juengste vor ref_year (bzw. <= ref_year mit
    allow_same_year).

    Rueckgabe: (gewaehlt {kachel: info inkl. 'year'}, fehlend [kacheln ohne Item],
    ohne_passendes_jahr {kachel: [vorhandene Jahre]})."""
    ref_year = int(ref_year)
    chosen, missing, no_year = {}, [], {}
    for key in needed:
        years = sorted(available.get(key, {}))
        if not years:
            missing.append(key)
            continue
        eligible = [y for y in years if y < ref_year or (allow_same_year and y == ref_year)]
        if not eligible:
            no_year[key] = years
            continue
        y = max(eligible)
        chosen[key] = dict(available[key][y], year=y)
    return chosen, missing, no_year


def search(session, bounds, ref_year, allow_same_year=False, cancel=None):
    """Kachelsuche fuer den Perimeter (LV95-Bounds von DSM aktuell). Liefert eine
    JSON-taugliche Zusammenfassung, die auch das GUI anzeigt."""
    needed = tiles_for_extent(bounds)
    bbox = extent_to_wgs84_bbox(bounds)
    logger.info(f"swissSURFACE3D: {len(needed)} Kachel(n) im Perimeter, Suche in "
                f"{S3D_COLLECTION} (BBox WGS84 {bbox})")
    available = parse_items(search_items(session, bbox, cancel))
    chosen, missing, no_year = choose_years(available, needed, ref_year, allow_same_year)
    check_height_reference(chosen)
    years = sorted({c["year"] for c in chosen.values()})
    result = {
        "ref_year": int(ref_year),
        "allow_same_year": bool(allow_same_year),
        "needed": needed,
        "available": {k: sorted(v) for k, v in available.items() if k in needed},
        "chosen": chosen,
        "missing": missing,
        "no_suitable_year": no_year,
        "years": years,
        "mixed_years": len(years) > 1,
    }
    rule = "<=" if allow_same_year else "<"
    logger.info(f"  Jahreswahl: juengstes Jahr {rule} {ref_year} -> "
                + (", ".join(f"{y} ({sum(1 for c in chosen.values() if c['year'] == y)} Kachel(n))"
                             for y in years) or "keine Kachel"))
    if len(chosen) <= 20:
        for key in needed:
            if key in chosen:
                avail = result["available"].get(key, [])
                logger.info(f"    {key}: {chosen[key]['year']}  (vorhanden: "
                            f"{', '.join(str(y) for y in avail)})")
    else:
        # Grosse Perimeter: zusammengefasst nach gewaehltem Jahr und vorhandenen Jahren
        groups = {}
        for key, c in chosen.items():
            groups.setdefault((c["year"], tuple(result["available"].get(key, []))), []).append(key)
        for (year, avail), keys in sorted(groups.items()):
            logger.info(f"    {year} gewaehlt (vorhanden: {', '.join(map(str, avail))}): "
                        f"{len(keys)} Kachel(n)")
    if missing:
        logger.warning(f"swissSURFACE3D: {len(missing)} Kachel(n) im Perimeter gibt es nicht "
                       f"- das Diff ist dort NoData: {', '.join(missing)}")
    if no_year:
        logger.warning(f"swissSURFACE3D: {len(no_year)} Kachel(n) ohne Jahr {rule} {ref_year} "
                       f"- das Diff ist dort NoData: "
                       + ", ".join(f"{k} (nur {', '.join(str(y) for y in v)})"
                                   for k, v in no_year.items()))
    if result["mixed_years"]:
        logger.warning(f"swissSURFACE3D: GEMISCHTE JAHRE im Perimeter ({', '.join(map(str, years))})"
                       f" - die Referenz ist nicht einheitlich; die Jahre stehen in den "
                       f"TIFF-Metadaten und in der Asset-Beschreibung.")
    return result


def download_tiles(session, chosen, cache_dir, cancel=None, progress=None):
    """Laedt die gewaehlten Kachel-COGs in den Cache (_stac_http.download_file:
    Pruefsumme, Wiederholung). Vorhandene Dateien mit passender Pruefsumme werden
    wiederverwendet. Gibt {kachel: pfad} zurueck. progress(anteil 0..1, text) optional."""
    from _stac_http import download_file

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    keys = sorted(chosen)
    counts = {"cache": 0, "geladen": 0}
    for i, key in enumerate(keys, 1):
        info = chosen[key]
        dest = cache_dir / os.path.basename(info["href"])
        cancel_fn = (lambda: _cancelled(cancel)) if cancel is not None else None
        counts[download_file(session, info["href"], dest, info.get("checksum", ""), cancel_fn)] += 1
        paths[key] = str(dest)
        if progress:
            progress(i / len(keys), f"Kachel {i}/{len(keys)}")
    logger.info(f"  Kacheln: {counts['geladen']} geladen, {counts['cache']} aus dem Cache ({cache_dir})")
    return paths


def snapped_bounds(bounds, res=S3D_GSD_M):
    """Extent nach aussen auf das Kachelgitter gerundet (Ursprung = Kilometerlinie)."""
    minx, miny, maxx, maxy = bounds
    return (math.floor(minx / res + 1e-9) * res, math.floor(miny / res + 1e-9) * res,
            math.ceil(maxx / res - 1e-9) * res, math.ceil(maxy / res - 1e-9) * res)


def build_vrt(tile_paths, bounds, res, src_nodata, dst_nodata, vrt_path):
    """VRT ueber die Kacheln auf genau 'bounds' (bereits aufs Kachelgitter gerundet),
    ohne Resampling. Pixel mit src_nodata und Flaechen ohne Kachel tragen dst_nodata."""
    from osgeo import gdal
    gdal.UseExceptions()
    opts = gdal.BuildVRTOptions(outputBounds=bounds, xRes=res, yRes=res,
                                srcNodata=src_nodata, VRTNodata=dst_nodata,
                                resampleAlg="nearest")
    ds = gdal.BuildVRT(str(vrt_path), list(tile_paths), options=opts)
    if ds is None:
        raise RuntimeError("gdal.BuildVRT hat None zurueckgegeben (swissSURFACE3D-Mosaik)")
    ds.GetRasterBand(1).SetUnitType("metre")
    ds.FlushCache()
    ds = None
    return str(vrt_path)
