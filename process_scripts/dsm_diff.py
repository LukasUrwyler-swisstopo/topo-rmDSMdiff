"""
dsm_diff.py - Differenzraster dh = DSM aktuell - DSM alt als Cloud Optimized GeoTIFF.

Kernfunktion run_dsm_diff(...) ohne GUI-Abhaengigkeit, CLI am Dateiende
(python dsm_diff.py -h). Braucht nur osgeo (gdal/osr), numpy und die Standardbibliothek;
fuer swissSURFACE3D zusaetzlich requests (ueber _stac_http).

Methode
  - Referenzgitter = DSM aktuell: gleiche Ausdehnung, gleiche Geotransformation. So liegt
    das Diff pixelgenau auf dem DSM-RASTER-Asset desselben STAC-Items.
  - DSM alt im selben Gitter (gleiche Zellgroesse, Ursprungsversatz ganzzahlig innerhalb
    1e-3 Pixel) wird direkt gelesen. Sonst gdal.Warp auf das Referenzgitter ins Staging:
    bilinear, wenn DSM alt groeber oder nur versetzt ist, average, wenn es feiner ist.
  - Warp immer mit srcSRS = dstSRS = EPSG:2056: bei zwei Compound-CRS (z.B. 2056+5728)
    loest GDAL sonst eine Hoehentransformation ueber PROJ-Grids aus und veraendert die
    Z-Werte still (im DMC-Converter so gemessen).
  - NoData, wo eines der beiden DSM NoData/NaN hat - ausgewertet wird das Maskenband,
    nicht nur der NoData-Wert. Gerechnet wird in float64, geschrieben Float32 mit
    NoData -3.4028234663852886e+38 (= -FLT_MAX, wie DSM-RASTER / GDWH SB_DSM).
  - Blockweise (Standard 2048 x 2048), Zwischendatei unkomprimiert im Staging, danach COG
    (DEFLATE, PREDICTOR=FLOATING_POINT), Pruefung, erst dann os.replace an den Zielort.
  - CRS-Tag des Diff nur EPSG:2056: dh ist eine Hoehendifferenz, keine LN02-Hoehe. Der
    Hoehenbezug der Quellen steht in den TIFF-Metadaten.

Es gibt bewusst KEINE Volumenberechnung: ein Volumen haengt am Perimeter. Der Nutzer
rechnet es mit DSM, Diff und seinem eigenen Polygon selbst.
"""

import argparse
import datetime
import logging
import math
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

import swisssurface3d as s3d

gdal.UseExceptions()
osr.UseExceptions()

logger = logging.getLogger("dsm_diff")

DIFF_NODATA = -3.4028234663852886e+38          # -FLT_MAX, wie LAS_RASTER_NODATA (DMC)
GDAL_MIN_VERSION = 3010000                    # COG-Treiber ab GDAL 3.1
GRID_TOL_PX = 1e-3                            # zulaessiger Ursprungsversatz in Pixel
HUGE_ABS = 1e30                               # |Wert| darueber = ungetaggter Sentinel
UNIT_OK = {"", "m", "metre", "meter", "metres", "meters"}
HEIGHT_REF_NAMES = {5728: "LN02", 5729: "LHN95"}
# Hoehenbezug im Dateinamen: ..._LV95_LN02.tif, ..._LV95_LHN95.tif, ..._2056_5728.tif
NAME_HEIGHT_PATTERN = re.compile(r"_(?:LV95_(LN02|LHN95)|2056_(5728|5729))(?=[_.]|$)",
                                 re.IGNORECASE)

# Zwischendateien im Staging: Wegwerfprodukte, Kompression kostet dort nur CPU
STAGING_OPTIONS = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=NONE",
                   "BIGTIFF=IF_SAFER"]
# PREDICTOR=FLOATING_POINT statt 3: '3' ist erst in neueren GDAL ein Alias.
# Ohne COMPRESS-Angabe wuerde der COG-Treiber LZW nehmen.
COG_OPTIONS = ["COMPRESS=DEFLATE", "PREDICTOR=FLOATING_POINT", "BLOCKSIZE=512",
               "OVERVIEWS=AUTO", "RESAMPLING=AVERAGE", "BIGTIFF=IF_SAFER",
               "NUM_THREADS=ALL_CPUS"]

ALT_S3D = "swisssurface3d"
ALT_FILE = "datei"

_LV95 = osr.SpatialReference()
_LV95.ImportFromEPSG(2056)


# ─── Abbruch und Fortschritt ──────────────────────────────────────────────────
def _cancelled(cancel):
    if cancel is None:
        return False
    return cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel())


def _check_cancel(cancel, where):
    if _cancelled(cancel):
        raise InterruptedError(f"Abbruch durch den Benutzer ({where})")


def _gdal_callback(progress, cancel, lo, hi, text):
    """GDAL-Callback: Fortschritt in [lo, hi] Prozent, Rueckgabe 0 bricht GDAL ab."""
    def cb(complete, message, data):
        if _cancelled(cancel):
            return 0
        if progress:
            progress(lo + (hi - lo) * float(complete or 0), text)
        return 1
    return cb


def _run_gdal(cancel, where, fn, *args, **kwargs):
    """GDAL-Aufruf; ein Callback-Abbruch kommt als InterruptedError heraus."""
    try:
        return fn(*args, **kwargs)
    except RuntimeError:
        if _cancelled(cancel):
            raise InterruptedError(f"Abbruch durch den Benutzer ({where})") from None
        raise


def check_gdal_version():
    num = int(gdal.VersionInfo("VERSION_NUM"))
    logger.info(f"GDAL {gdal.__version__}")
    if num < GDAL_MIN_VERSION:
        raise RuntimeError(f"GDAL {gdal.__version__} ist zu alt - mindestens 3.1 noetig "
                           f"(COG-Treiber).")
    return num


# ─── CRS und Hoehenbezug ──────────────────────────────────────────────────────
def horizontal_is_lv95(srs):
    """True, wenn der horizontale Teil von srs EPSG:2056 (LV95) ist."""
    h = srs.Clone()
    if h.IsCompound():
        if hasattr(h, "StripVertical"):              # GDAL >= 3.6
            h.StripVertical()
        else:                                        # Fallback aeltere GDAL
            return (h.GetAuthorityName("PROJCS") == "EPSG"
                    and h.GetAuthorityCode("PROJCS") == "2056")
    try:
        if h.IsSame(_LV95, ["IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES"]):
            return True
    except TypeError:                                # IsSame ohne Optionen (alt)
        if h.IsSame(_LV95):
            return True
    # z.B. ESRI-WKT aus einer .prj ohne Authority: ueber die EPSG-Identifikation
    try:
        h.AutoIdentifyEPSG()
    except Exception:
        pass
    return h.GetAuthorityName(None) == "EPSG" and h.GetAuthorityCode(None) == "2056"


def vertical_epsg(srs):
    """EPSG-Code des Hoehenbezugs aus dem CRS-Tag (Compound-CRS), sonst None."""
    if srs is None or not srs.IsCompound():
        return None
    code = srs.GetAuthorityCode("VERT_CS")
    if code and code.isdigit():
        return int(code)
    name = srs.GetAttrValue("VERT_CS") or ""
    for epsg, token in HEIGHT_REF_NAMES.items():
        if token.lower() in name.lower():
            return epsg
    return None


def height_ref_from_name(path):
    """Hoehenbezug aus dem Dateinamen (_LV95_LN02, _LV95_LHN95, _2056_5728, _2056_5729)."""
    hits = set()
    for m in NAME_HEIGHT_PATTERN.finditer(os.path.basename(str(path))):
        token = (m.group(1) or "").upper()
        hits.add({"LN02": 5728, "LHN95": 5729}[token] if token else int(m.group(2)))
    if len(hits) > 1:
        raise ValueError(f"Dateiname nennt zwei Hoehenbezuege: {os.path.basename(path)}")
    return hits.pop() if hits else None


def height_ref_label(epsg):
    if epsg is None:
        return "unbekannt"
    return f"{HEIGHT_REF_NAMES.get(epsg, 'EPSG')} (EPSG:{epsg})"


def resolve_height_ref(facts):
    """(epsg, quelle) mit quelle 'tag' | 'stac' | 'name' | None.

    Quellen der Reihe nach: CRS-Tag, STAC-Beschreibung (SourceReferenceSystem ..._LN02 bei
    einem STAC-Link, zaehlt wie ein Tag), Dateiname (nur Zusatzindiz, Entscheid 8).
    Widersprechen sich zwei Quellen -> ValueError."""
    sources = [("CRS-Tag", "tag", facts.get("vertical_tag")),
               ("STAC-Beschreibung", "stac", facts.get("vertical_stac")),
               ("Dateiname", "name", facts.get("vertical_name"))]
    known = [(label, key, v) for label, key, v in sources if v is not None]
    if len({v for _, _, v in known}) > 1:
        raise ValueError(
            f"{facts['role']}: Hoehenbezug widerspruechlich - "
            + ", ".join(f"{label} {height_ref_label(v)}" for label, _, v in known)
            + f" ({facts['name']}). Bitte klaeren, welcher stimmt - das Werkzeug kann nur "
              f"Tags, STAC-Angaben und Namen vergleichen, nicht Inhalte.")
    return (known[0][2], known[0][1]) if known else (None, None)


def check_height_refs(neu, alt):
    """Gleicher Hoehenbezug? Verschieden -> ValueError. Ungeprueft -> Warnung.
    (LN02 - LHN95 gemessen -0.06 bis -0.30 m - auf einem Gletscher kein Rauschen.)"""
    (e_neu, how_neu), (e_alt, how_alt) = resolve_height_ref(neu), resolve_height_ref(alt)
    neu["height_ref"], neu["height_ref_source"] = e_neu, how_neu
    alt["height_ref"], alt["height_ref_source"] = e_alt, how_alt
    if e_neu is not None and e_alt is not None and e_neu != e_alt:
        raise ValueError(
            f"Hoehenbezug verschieden: DSM aktuell {height_ref_label(e_neu)} ({how_neu}), "
            f"DSM alt {height_ref_label(e_alt)} ({how_alt}). LN02 - LHN95 liegt bei -0.06 bis "
            f"-0.30 m - das Diff waere systematisch verfaelscht. Abbruch.")
    if e_neu is None or e_alt is None:
        ohne = [f["role"] for f in (neu, alt) if f["height_ref"] is None]
        logger.warning(f"Gleicher Hoehenbezug NICHT geprueft: {' und '.join(ohne)} ohne "
                       f"Hoehenbezug im CRS-Tag, in der STAC-Beschreibung und im Dateinamen.")
    elif "name" in (how_neu, how_alt):
        nur_name = [f["role"] for f in (neu, alt) if f["height_ref_source"] == "name"]
        logger.warning(f"Hoehenbezug {height_ref_label(e_neu)} nur aus dem Dateinamen "
                       f"geprueft ({', '.join(nur_name)} ohne Hoehenbezug im CRS-Tag).")
    else:
        quellen = {"tag": "CRS-Tag", "stac": "STAC-Beschreibung"}
        logger.info(f"Hoehenbezug geprueft: beide {height_ref_label(e_neu)} (DSM aktuell: "
                    f"{quellen[how_neu]}, DSM alt: {quellen[how_alt]})")


# ─── Raster pruefen ───────────────────────────────────────────────────────────
def describe_raster(path):
    """Info fuer das GUI (Aktion 'info'), ohne abzubrechen: Felder + 'problem'."""
    info = {"path": str(path), "problem": None}
    try:
        f = raster_facts(path, "Datei")
        info.update({k: v for k, v in f.items() if k != "gt"})
        info["gt"] = list(f["gt"])
    except Exception as e:
        info["problem"] = str(e)
        try:
            ds = gdal.Open(str(path), gdal.GA_ReadOnly)
            b = ds.GetRasterBand(1)
            gt = ds.GetGeoTransform()
            srs = ds.GetSpatialRef()
            info.update(width=ds.RasterXSize, height=ds.RasterYSize, bands=ds.RasterCount,
                        res_x=gt[1], res_y=abs(gt[5]), srs_name=srs.GetName() if srs else None,
                        nodata=b.GetNoDataValue(), dtype=gdal.GetDataTypeName(b.DataType))
            ds = None
        except Exception:
            pass
    try:
        info["size_mb"] = round(os.path.getsize(path) / 1048576, 1)
    except OSError:
        pass
    # Metadaten eines mit diesem Werkzeug erstellten DIFF (GUI: 'Nur STAC-Import' belegt
    # daraus Area, LineIDs, Auftragstyp vor)
    try:
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        md = ds.GetMetadata() or {}
        ds = None
        info["dsmdiff"] = {k: v for k, v in md.items() if k.startswith("DSMDIFF_")}
    except Exception:
        info["dsmdiff"] = {}
    return info


def raster_facts(path, role):
    """Liest und prueft ein Hoehenraster. ValueError mit verstaendlicher Meldung bei:
    nicht genau ein Band, kein/kein projiziertes CRS, horizontal nicht LV95, rotiert,
    nicht north-up, Einheit nicht Meter."""
    path = str(path)
    if not os.path.isfile(path):
        raise ValueError(f"{role}: Datei nicht gefunden: {path}")
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    try:
        name = os.path.basename(path)
        if ds.RasterCount != 1:
            raise ValueError(f"{role}: {ds.RasterCount} Baender - erwartet genau ein Band ({name})")
        gt = ds.GetGeoTransform(can_return_null=True)
        if gt is None:
            raise ValueError(f"{role}: keine Georeferenzierung ({name})")
        if gt[2] != 0 or gt[4] != 0:
            raise ValueError(f"{role}: Raster ist rotiert (Geotransformation {gt}) - nicht "
                             f"unterstuetzt ({name})")
        if gt[1] <= 0 or gt[5] >= 0:
            raise ValueError(f"{role}: Raster ist nicht north-up (Geotransformation {gt}) "
                             f"({name})")
        srs = ds.GetSpatialRef()
        if srs is None:
            raise ValueError(f"{role}: kein CRS in der Datei ({name}) - erwartet EPSG:2056 "
                             f"(LV95). Es wird nichts angenommen.")
        if not srs.IsProjected():
            raise ValueError(f"{role}: CRS '{srs.GetName()}' ist nicht projiziert ({name})")
        if not horizontal_is_lv95(srs):
            raise ValueError(f"{role}: horizontales CRS ist '{srs.GetName()}', erwartet EPSG:2056 "
                             f"(LV95). Das Werkzeug projiziert nie um - bitte die Quelle "
                             f"pruefen ({name}).")
        b = ds.GetRasterBand(1)
        unit = (b.GetUnitType() or "").strip()
        if unit.lower() not in UNIT_OK:
            raise ValueError(f"{role}: Einheit '{unit}' - erwartet Meter oder leer ({name})")
        scale = b.GetScale()
        offset = b.GetOffset()
        w, h = ds.RasterXSize, ds.RasterYSize
        facts = {
            "path": path, "name": name, "role": role,
            "width": w, "height": h, "gt": tuple(gt),
            "res_x": gt[1], "res_y": -gt[5],
            "bounds": (gt[0], gt[3] + h * gt[5], gt[0] + w * gt[1], gt[3]),
            "srs_name": srs.GetName(), "compound": bool(srs.IsCompound()),
            "vertical_tag": vertical_epsg(srs), "vertical_name": height_ref_from_name(path),
            "vertical_stac": None, "source_url": None,
            "nodata": b.GetNoDataValue(), "dtype": gdal.GetDataTypeName(b.DataType),
            "unit": unit,
            "scale": 1.0 if scale is None else float(scale),
            "offset": 0.0 if offset is None else float(offset),
            "mask_flags": b.GetMaskFlags(),
        }
    finally:
        ds = None
    return facts


def log_facts(f):
    nd = "kein NoData-Tag" if f["nodata"] is None else f"NoData {f['nodata']!r}"
    logger.info(f"{f['role']}: {f['name']}")
    logger.info(f"  {f['width']} x {f['height']} px, Zelle {f['res_x']:g} x {f['res_y']:g} m, "
                f"{f['dtype']}, {nd}")
    logger.info(f"  CRS {f['srs_name']}  |  Hoehenbezug Tag: {height_ref_label(f['vertical_tag'])}"
                + (f", STAC: {height_ref_label(f['vertical_stac'])}" if f.get("source_url") else "")
                + f", Name: {height_ref_label(f['vertical_name'])}")
    logger.info(f"  Extent {f['bounds'][0]:.2f}, {f['bounds'][1]:.2f} - "
                f"{f['bounds'][2]:.2f}, {f['bounds'][3]:.2f}")
    if f["scale"] != 1.0 or f["offset"] != 0.0:
        logger.info(f"  Scale/Offset {f['scale']:g} / {f['offset']:g} - wird angewendet "
                    f"(Hoehe = Wert x Scale + Offset)")


def _intersection(a, b):
    minx, miny = max(a[0], b[0]), max(a[1], b[1])
    maxx, maxy = min(a[2], b[2]), min(a[3], b[3])
    return (minx, miny, maxx, maxy) if minx < maxx and miny < maxy else None


def grid_relation(neu, alt):
    """Wie liegt DSM alt zum Gitter von DSM aktuell?

    {'method': 'none', 'col_off', 'row_off'}: gleiches Gitter, direkt lesen (alt-Pixel =
    neu-Pixel + Versatz). Sonst {'method': 'bilinear'|'average', 'reason'} (Entscheid 5)."""
    same_res = (math.isclose(alt["res_x"], neu["res_x"], rel_tol=1e-9, abs_tol=1e-12) and
                math.isclose(alt["res_y"], neu["res_y"], rel_tol=1e-9, abs_tol=1e-12))
    if same_res:
        cx = (neu["gt"][0] - alt["gt"][0]) / alt["res_x"]
        cy = (alt["gt"][3] - neu["gt"][3]) / alt["res_y"]
        if abs(cx - round(cx)) <= GRID_TOL_PX and abs(cy - round(cy)) <= GRID_TOL_PX:
            return {"method": "none", "col_off": int(round(cx)), "row_off": int(round(cy)),
                    "reason": "gleiches Gitter, kein Resampling"}
        return {"method": "bilinear",
                "reason": f"gleiche Zellgroesse, Ursprung um {cx - round(cx):+.3f} / "
                          f"{cy - round(cy):+.3f} Pixel versetzt"}
    r_neu, r_alt = max(neu["res_x"], neu["res_y"]), max(alt["res_x"], alt["res_y"])
    if r_alt > r_neu:
        return {"method": "bilinear",
                "reason": f"DSM alt groeber ({r_alt:g} m) als DSM aktuell ({r_neu:g} m)"}
    return {"method": "average",
            "reason": f"DSM alt feiner ({r_alt:g} m) als DSM aktuell ({r_neu:g} m)"}


# ─── Rechnen ──────────────────────────────────────────────────────────────────
def warp_to_grid(alt, neu, dst_path, method, cancel=None, progress=None, lo=0.0, hi=100.0):
    """DSM alt auf das Gitter von DSM aktuell (Float32, NoData -FLT_MAX, unkomprimiert).
    Rohwerte werden interpoliert; Scale/Offset von DSM alt gelten danach unveraendert
    (lineare Abbildung, vertauschbar mit bilinear/average)."""
    minx, miny, maxx, maxy = neu["bounds"]
    opts = gdal.WarpOptions(
        format="GTiff", outputBounds=(minx, miny, maxx, maxy),
        width=neu["width"], height=neu["height"],
        # Beide explizit 2056: sonst Hoehentransformation zwischen Compound-CRS
        srcSRS="EPSG:2056", dstSRS="EPSG:2056",
        resampleAlg=method, outputType=gdal.GDT_Float32,
        srcNodata=alt["nodata"], dstNodata=DIFF_NODATA,
        multithread=True, warpOptions=["NUM_THREADS=ALL_CPUS"],
        creationOptions=STAGING_OPTIONS,
        callback=_gdal_callback(progress, cancel, lo, hi, f"Resampling ({method})"))
    out = _run_gdal(cancel, "Resampling", gdal.Warp, str(dst_path), alt["path"], options=opts)
    if out is None:
        raise RuntimeError("gdal.Warp hat None zurueckgegeben (Resampling DSM alt)")
    out.FlushCache()
    out = None
    return str(dst_path)


def _read_block(band, xoff, yoff, w, h, scale, offset, all_valid):
    """(werte float64, gueltig bool) - gueltig = Maskenband > 0 und endlich."""
    data = band.ReadAsArray(xoff, yoff, w, h).astype(np.float64)
    valid = np.isfinite(data)
    if not all_valid:
        valid &= band.GetMaskBand().ReadAsArray(xoff, yoff, w, h) > 0
    huge = valid & (np.abs(data) > HUGE_ABS)
    n_huge = int(huge.sum())
    if n_huge:
        valid &= ~huge
    if scale != 1.0 or offset != 0.0:
        data = data * scale + offset
    return data, valid, n_huge


def _merge_stats(acc, x):
    """Laufende Statistik (Chan et al.): n, Mittel, M2, Min, Max - blockweise kombiniert."""
    n_b = x.size
    if n_b == 0:
        return
    mean_b = float(x.mean())
    m2_b = float(((x - mean_b) ** 2).sum())
    n_a = acc["n"]
    n = n_a + n_b
    delta = mean_b - acc["mean"]
    acc["mean"] += delta * n_b / n
    acc["m2"] += m2_b + delta * delta * n_a * n_b / n
    acc["n"] = n
    acc["min"] = min(acc["min"], float(x.min()))
    acc["max"] = max(acc["max"], float(x.max()))


def compute_diff(neu, alt, col_off, row_off, out_path, block_size=2048, cancel=None,
                 progress=None, lo=0.0, hi=100.0, alt_sentinel=None):
    """dh = neu - alt blockweise ins Gitter von DSM aktuell (Float32, NoData -FLT_MAX).
    alt-Pixel = neu-Pixel + (col_off, row_off). alt_sentinel: Wert, der in DSM alt nicht
    als gueltig vorkommen darf (Mosaik: -9999 muss zu NoData geworden sein)."""
    W, H = neu["width"], neu["height"]
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(str(out_path), W, H, 1, gdal.GDT_Float32, STAGING_OPTIONS)
    out.SetGeoTransform(neu["gt"])
    out.SetSpatialRef(_LV95)
    ob = out.GetRasterBand(1)
    ob.SetNoDataValue(DIFF_NODATA)

    nds = gdal.Open(neu["path"], gdal.GA_ReadOnly)
    ads = gdal.Open(alt["path"], gdal.GA_ReadOnly)
    nb, ab = nds.GetRasterBand(1), ads.GetRasterBand(1)
    n_all_valid = bool(nb.GetMaskFlags() & gdal.GMF_ALL_VALID)
    a_all_valid = bool(ab.GetMaskFlags() & gdal.GMF_ALL_VALID)
    AW, AH = ads.RasterXSize, ads.RasterYSize

    acc = {"n": 0, "mean": 0.0, "m2": 0.0, "min": math.inf, "max": -math.inf}
    n_neu_valid = n_no_ref = n_huge = n_sentinel = 0
    bs = max(1, int(block_size))
    n_blocks = math.ceil(W / bs) * math.ceil(H / bs)
    done = 0
    try:
        for yoff in range(0, H, bs):
            h = min(bs, H - yoff)
            for xoff in range(0, W, bs):
                _check_cancel(cancel, "Differenzrechnung")
                w = min(bs, W - xoff)
                nd, nv, nh = _read_block(nb, xoff, yoff, w, h, neu["scale"], neu["offset"],
                                         n_all_valid)
                ad = np.zeros((h, w), np.float64)
                av = np.zeros((h, w), bool)
                ax0, ay0 = xoff + col_off, yoff + row_off
                ix0, iy0 = max(ax0, 0), max(ay0, 0)
                ix1, iy1 = min(ax0 + w, AW), min(ay0 + h, AH)
                if ix1 > ix0 and iy1 > iy0:
                    d, v, ah = _read_block(ab, ix0, iy0, ix1 - ix0, iy1 - iy0,
                                           alt["scale"], alt["offset"], a_all_valid)
                    sl = (slice(iy0 - ay0, iy1 - ay0), slice(ix0 - ax0, ix1 - ax0))
                    ad[sl], av[sl] = d, v
                    nh += ah
                if alt_sentinel is not None:
                    hit = av & (ad == alt_sentinel)
                    n_sentinel += int(hit.sum())
                valid = nv & av
                dh = np.full((h, w), DIFF_NODATA, np.float32)
                vals = (nd[valid] - ad[valid]).astype(np.float32)
                dh[valid] = vals
                ob.WriteArray(dh, xoff, yoff)
                _merge_stats(acc, vals.astype(np.float64))
                n_neu_valid += int(nv.sum())
                n_no_ref += int((nv & ~av).sum())
                n_huge += nh
                done += 1
                if progress:
                    progress(lo + (hi - lo) * done / n_blocks, "Differenzrechnung")
        ob.FlushCache()
    finally:
        ob = out = nb = ab = None
        nds = ads = None

    if n_sentinel:
        raise RuntimeError(f"DSM alt enthaelt {n_sentinel} Pixel mit dem Wert {alt_sentinel:g}, "
                           f"die als gueltig gelten - NoData der Kacheln wurde nicht umgesetzt.")
    total = W * H
    stats = {
        "valid": acc["n"], "total": total,
        "valid_percent": 100.0 * acc["n"] / total if total else 0.0,
        "min": acc["min"] if acc["n"] else None, "max": acc["max"] if acc["n"] else None,
        "mean": acc["mean"] if acc["n"] else None,
        "std": math.sqrt(acc["m2"] / acc["n"]) if acc["n"] else None,
        "neu_valid": n_neu_valid, "no_reference": n_no_ref,
        "no_reference_percent": 100.0 * n_no_ref / n_neu_valid if n_neu_valid else 0.0,
        "huge_values_masked": n_huge,
    }
    return stats


def write_cog(src_path, dst_path, cancel=None, progress=None, lo=0.0, hi=100.0,
              output_srs=None, text="COG schreiben"):
    kw = {"outputSRS": output_srs} if output_srs else {}
    opts = gdal.TranslateOptions(format="COG", creationOptions=COG_OPTIONS,
                                 callback=_gdal_callback(progress, cancel, lo, hi, text), **kw)
    out = _run_gdal(cancel, "COG schreiben", gdal.Translate, str(dst_path), str(src_path),
                    options=opts)
    if out is None:
        raise RuntimeError("gdal.Translate (COG) hat None zurueckgegeben")
    out = None


def check_cog(path, width, height, gt, vertical=None, require_metadata=(),
              require_stats=False):
    """Kontrolle eines fertigen COG. Leere Liste = in Ordnung.
    vertical None: kein Hoehenbezug im Tag erlaubt (Diff); sonst erwarteter EPSG (Mosaik)."""
    problems = []
    old_pam = gdal.GetConfigOption("GDAL_PAM_ENABLED")
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")     # Lesen soll keine aux.xml anlegen
    try:
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        st = ds.GetMetadata("IMAGE_STRUCTURE") or {}
        if st.get("LAYOUT") != "COG":
            problems.append(f"Layout {st.get('LAYOUT')!r} statt COG")
        if (st.get("COMPRESSION") or "").upper() != "DEFLATE":
            problems.append(f"Kompression {st.get('COMPRESSION')!r} statt DEFLATE")
        if st.get("PREDICTOR") != "3":
            problems.append(f"Predictor {st.get('PREDICTOR')!r} statt 3 (FLOATING_POINT)")
        b = ds.GetRasterBand(1)
        if b.DataType != gdal.GDT_Float32:
            problems.append(f"Datentyp {gdal.GetDataTypeName(b.DataType)} statt Float32")
        nd = b.GetNoDataValue()
        if nd is None or struct.pack("<f", nd) != struct.pack("<f", DIFF_NODATA):
            problems.append(f"NoData {nd!r} statt {DIFF_NODATA!r}")
        if (ds.RasterXSize, ds.RasterYSize) != (width, height):
            problems.append(f"Groesse {ds.RasterXSize} x {ds.RasterYSize} statt {width} x {height}")
        if tuple(ds.GetGeoTransform()) != tuple(gt):
            problems.append(f"Geotransformation {ds.GetGeoTransform()} statt {tuple(gt)}")
        srs = ds.GetSpatialRef()
        if srs is None or not horizontal_is_lv95(srs):
            problems.append(f"CRS {srs.GetName() if srs else 'fehlt'} statt LV95")
        elif vertical is None and srs.IsCompound():
            problems.append(f"CRS {srs.GetName()} traegt einen Hoehenbezug - erwartet nur EPSG:2056")
        elif vertical is not None and vertical_epsg(srs) != vertical:
            problems.append(f"Hoehenbezug {vertical_epsg(srs)} statt {vertical}")
        if b.GetUnitType() != "metre":
            problems.append(f"Einheit {b.GetUnitType()!r} statt 'metre'")
        if max(width, height) > 512 and b.GetOverviewCount() < 1:
            problems.append("keine Overviews")
        md = ds.GetMetadata() or {}
        missing = [k for k in require_metadata if not md.get(k)]
        if missing:
            problems.append(f"Metadaten fehlen: {', '.join(missing)}")
        if require_stats and b.GetMetadataItem("STATISTICS_MEAN") is None:
            problems.append("Statistik fehlt")
        b = ds = None
    finally:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", old_pam)
    if os.path.exists(str(path) + ".aux.xml"):
        problems.append("aux.xml neben der Datei")
    return problems


def _set_diff_metadata(path, items, stats):
    """Metadaten, Banddescription, Einheit und Statistik ins Staging-TIFF (Update-Modus,
    landet im TIFF selbst und wird vom COG-Schritt uebernommen - keine aux.xml)."""
    ds = gdal.Open(str(path), gdal.GA_Update)
    ds.SetMetadata({k: str(v) for k, v in items.items()})
    b = ds.GetRasterBand(1)
    b.SetDescription("dh = DSM new - DSM old [m]")
    b.SetUnitType("metre")
    if stats["valid"]:
        b.SetStatistics(stats["min"], stats["max"], stats["mean"], stats["std"])
        b.SetMetadataItem("STATISTICS_VALID_PERCENT", f"{stats['valid_percent']:.4f}")
    b.FlushCache()
    b = ds = None


def _tmp_path(path):
    stem, ext = os.path.splitext(str(path))
    return f"{stem}_tmp{ext}"


def _remove(path):
    try:
        if path and os.path.isfile(str(path)):
            os.remove(str(path))
    except OSError as e:
        logger.warning(f"Datei nicht loeschbar: {path} ({e})")


def _same_file(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _is_url(path):
    return str(path or "").strip().lower().startswith(("http://", "https://"))


def _check_paths(dsm_neu, out, dsm_alt, alt_quelle, mosaic_out, overwrite):
    """Formale Pruefung von Ein- und Ausgaben. Eingaben duerfen STAC-Links sein (Form
    geprueft hier, Inhalt beim Laden)."""
    import stac_import
    checks = [("DSM aktuell", dsm_neu)] + ([("DSM alt", dsm_alt)] if alt_quelle == ALT_FILE else [])
    for role, p in checks:
        if not p:
            raise ValueError(f"{role} fehlt.")
        if _is_url(p):
            stac_import.parse_asset_href(p)
        elif not os.path.isfile(str(p)):
            raise ValueError(f"{role} nicht gefunden: {p}")
    outputs = [("DIFF-COG", out)] + ([("Mosaik", mosaic_out)] if mosaic_out else [])
    inputs = [p for p in [dsm_neu] + ([dsm_alt] if alt_quelle == ALT_FILE else [])
              if not _is_url(p)]
    for label, p in outputs:
        if not str(p).lower().endswith(".tif"):
            raise ValueError(f"{label}: Endung .tif erwartet: {p}")
        if not os.path.isdir(os.path.dirname(os.path.abspath(str(p)))):
            raise ValueError(f"{label}: Ausgabeordner existiert nicht: {os.path.dirname(str(p))}")
        if any(_same_file(p, i) for i in inputs):
            raise ValueError(f"{label} darf nicht eine Eingabe sein: {p}")
        if os.path.exists(str(p)) and not overwrite:
            raise ValueError(f"{label} existiert bereits: {p}\n  Ueberschreiben nur mit "
                             f"overwrite=True (CLI: --overwrite; das GUI ersetzt immer).")
    if mosaic_out and _same_file(out, mosaic_out):
        raise ValueError("DIFF-COG und Mosaik duerfen nicht dieselbe Datei sein.")


STAC_ASSET_CACHE = "stac_asset_cache"


def resolve_input(path, role, staging_dir, session, cancel=None, progress=None):
    """Lokaler Pfad bleibt, wie er ist. STAC-Link: Asset-Metadaten holen, Datei in den Cache
    <staging>/stac_asset_cache/<item>/<asset> laden (Pruefsumme gegen checksum:multihash,
    gueltige Datei wird nicht neu geladen). -> (lokaler_pfad, stac_info | None)."""
    if not _is_url(path):
        return str(path), None
    import stac_import
    from _stac_http import download_file
    if session is None:
        from _stac_http import get_session
        session = get_session()
    info = stac_import.fetch_asset(session, path)
    dest = Path(staging_dir) / STAC_ASSET_CACHE / info["item"] / info["asset"]
    logger.info(f"{role}: STAC-Asset {info['asset']} (Item {info['item']}, {info['env']})"
                + (f", {info['size_mb']:.0f} MB" if info.get("size_mb") else ""))
    state = download_file(session, info["href"], dest, info["checksum"],
                          (lambda: _cancelled(cancel)) if cancel is not None else None, progress)
    logger.info(f"  {'aus dem Cache' if state == 'cache' else 'geladen'} (Pruefsumme OK): {dest}")
    return str(dest), info


HEADER_TRY_MB = (1, 8)        # erst 1 MB, notfalls 8 MB Dateikopf


def remote_header_facts(url, role, session, work_dir):
    """Gitter und Ausdehnung eines STAC-Links nur aus dem Dateikopf - fuer 'Kacheln suchen',
    damit dafuer nicht die ganze Datei (oft hunderte MB) geladen wird.

    Ein COG hat IFD und Tag-Daten am Dateianfang; GDAL liest Groesse, Geotransformation,
    CRS und NoData aus dem Kopf allein (am echten DSM-RASTER-Asset geprueft: 1 MB von
    594 MB genuegt). Gibt (facts, stac_info) zurueck, oder (None, stac_info), wenn auch
    8 MB nicht reichen (z.B. kein COG) - dann muss die ganze Datei geladen werden."""
    import stac_import
    info = stac_import.fetch_asset(session, url)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for mb in HEADER_TRY_MB:
        part = work_dir / f"kopf_{mb}MB_{info['asset']}"
        try:
            _download_head(session, info["href"], part, mb * 1048576)
            f = raster_facts(part, role)
            f["vertical_stac"] = info.get("height_ref")
            f["source_url"] = info.get("url")
            logger.info(f"{role}: Ausdehnung aus den ersten {mb} MB gelesen - die ganze Datei "
                        f"laedt erst der Lauf.")
            return f, info
        except Exception as e:
            logger.info(f"  {mb} MB Dateikopf reichen nicht ({str(e).splitlines()[0][:120]})")
        finally:
            _remove(part)
    return None, info


def _download_head(session, href, dest, nbytes):
    """Die ersten nbytes einer Datei (Range-Request; ignoriert der Server Range, wird nach
    nbytes abgebrochen)."""
    with session.get(href, headers={"Range": f"bytes=0-{nbytes - 1}"}, stream=True) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        got = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(262144):
                fh.write(chunk[: nbytes - got])
                got += len(chunk)
                if got >= nbytes:
                    break


def _facts_with_stac(local, role, info):
    """raster_facts + Angaben aus dem STAC (Hoehenbezug laut Beschreibung, Herkunft)."""
    f = raster_facts(local, role)
    if info:
        f["vertical_stac"] = info.get("height_ref")
        f["source_url"] = info.get("url")
    log_facts(f)
    return f


# ─── Kernfunktion ─────────────────────────────────────────────────────────────
def run_dsm_diff(dsm_neu, out_diff_cog, dsm_alt=None, alt_quelle=ALT_S3D,
                 staging_dir=None, overwrite=False, block_size=2048,
                 progress=None, cancel=None, *, ref_year=None, allow_same_year=False,
                 mosaic_out=None, session=None, s3d_result=None, keep_staging=False,
                 metadata=None, pre_check=None):
    """Berechnet dh = DSM aktuell - DSM alt als COG.

    dsm_neu / dsm_alt: lokaler Pfad oder STAC-Link (href oder API-URL eines Assets von INT/
        PROD) - ein Link wird zuerst in den Cache im Staging geladen.
    alt_quelle 'swisssurface3d' (Default): Kacheln aus dem STAC, Jahr je Kachel = das
        juengste VOR ref_year (Aufnahmejahr). allow_same_year (nur Kernfunktion/Tests)
        laesst das gleiche Jahr zu. Aus den Kacheln entsteht das Mosaik-COG mosaic_out
        (Originalgitter, NoData -FLT_MAX, EPSG:2056+5728) - die Basis fuer das Diff. Ohne
        mosaic_out landet es neben dem Diff (<stem>_swissSURFACE3D_50cm_LV95_LN02.tif).
    alt_quelle 'datei': eigenes DSM dsm_alt.
    session: requests-Session (Proxy) fuer swissSURFACE3D und STAC-Links; ohne Angabe
        ueber _stac_http.get_session().
    progress(prozent 0..100, text) und cancel (Event oder Callable) optional.
    metadata: zusaetzliche TIFF-Metadaten (z.B. LineIDs, Item-ID).
    pre_check(info): wird nach allen Pruefungen und VOR dem Schreiben der Ausgaben
        aufgerufen (z.B. STAC-Item pruefen). info = {'neu': facts, 's3d': suchergebnis | None}.
    Gibt ein dict mit Pfaden, Statistik und Quellenangaben zurueck."""
    if alt_quelle not in (ALT_S3D, ALT_FILE):
        raise ValueError(f"Unbekannte Quelle DSM alt: {alt_quelle!r}")
    if int(block_size) < 1:
        raise ValueError(f"Blockgroesse {block_size} ungueltig")
    check_gdal_version()
    if alt_quelle == ALT_S3D and not mosaic_out:
        stem = os.path.splitext(str(out_diff_cog))[0]
        mosaic_out = f"{stem}_swissSURFACE3D_50cm_LV95_LN02.tif"
    _check_paths(dsm_neu, out_diff_cog, dsm_alt, alt_quelle,
                 mosaic_out if alt_quelle == ALT_S3D else None, overwrite)
    staging_dir = Path(staging_dir) if staging_dir else Path(tempfile.gettempdir())
    needs_net = alt_quelle == ALT_S3D or _is_url(dsm_neu) or (alt_quelle == ALT_FILE and _is_url(dsm_alt))
    if needs_net and session is None:
        from _stac_http import get_session
        session = get_session()

    def dl_progress(lo, hi):
        return (lambda frac, text: progress(lo + (hi - lo) * frac, text)) if progress else None

    neu_local, neu_info = resolve_input(dsm_neu, "DSM aktuell", staging_dir, session, cancel,
                                        dl_progress(0, 10))
    neu = _facts_with_stac(neu_local, "DSM aktuell", neu_info)
    if neu["nodata"] is None:
        logger.warning("DSM aktuell traegt keinen NoData-Tag - alle Pixel gelten als gueltig "
                       "(ausser NaN und interne Maske).")

    alt = None
    if alt_quelle == ALT_FILE:
        alt_local, alt_info = resolve_input(dsm_alt, "DSM alt", staging_dir, session, cancel,
                                            dl_progress(10, 20))
        alt = _facts_with_stac(alt_local, "DSM alt", alt_info)
        check_height_refs(neu, alt)
        if _intersection(neu["bounds"], alt["bounds"]) is None:
            raise ValueError("DSM aktuell und DSM alt ueberlappen sich nicht.")
    else:
        if ref_year is None:
            raise ValueError("Aufnahmejahr fehlt - es bestimmt das swissSURFACE3D-Jahr je Kachel.")
        if s3d_result is None:
            s3d_result = s3d.search(session, neu["bounds"], ref_year, allow_same_year, cancel)
        if not s3d_result["chosen"]:
            raise ValueError(f"Keine swissSURFACE3D-Kachel mit passendem Jahr "
                             f"({'<=' if allow_same_year else '<'} {ref_year}) im Perimeter.")
        neu["height_ref"], neu["height_ref_source"] = resolve_height_ref(neu)
        if neu["height_ref"] not in (None, s3d.S3D_VERTICAL_EPSG):
            raise ValueError(f"Hoehenbezug verschieden: DSM aktuell "
                             f"{height_ref_label(neu['height_ref'])}, swissSURFACE3D LN02 "
                             f"(EPSG:5728). Abbruch.")

    if pre_check is not None:
        pre_check({"neu": neu, "s3d": s3d_result})

    # ── Ab hier entstehen Laufdateien und Ausgaben (vorher nur der Cache im Staging) ──
    stamp = datetime.datetime.now()
    run_dir = staging_dir / f"DSMDIFF_{Path(out_diff_cog).stem}_{stamp:%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=False)
    logger.info(f"Staging: {run_dir}")
    tmp_cog = _tmp_path(out_diff_cog)
    tmp_mosaic = _tmp_path(mosaic_out) if alt_quelle == ALT_S3D else None
    for p in (tmp_cog, tmp_mosaic):
        _remove(p)                                  # Rest eines abgebrochenen Laufs

    result = {"out": str(out_diff_cog), "mosaic": None, "alt_quelle": alt_quelle}
    try:
        alt_sentinel = None
        if alt_quelle == ALT_S3D:
            alt, alt_desc = _prepare_swisssurface3d(
                neu, s3d_result, session, staging_dir, run_dir, tmp_mosaic, cancel, progress)
            alt_sentinel = alt.pop("source_nodata", None)
            check_height_refs(neu, alt)
            if _intersection(neu["bounds"], alt["bounds"]) is None:
                raise ValueError("DSM aktuell und swissSURFACE3D-Mosaik ueberlappen sich nicht.")
            result["s3d"] = {"years": s3d_result["years"],
                             "tiles": {k: v["year"] for k, v in s3d_result["chosen"].items()},
                             "missing": s3d_result["missing"],
                             "no_suitable_year": s3d_result["no_suitable_year"]}
        else:
            alt_desc = alt.get("source_url") or alt["name"]

        rel = grid_relation(neu, alt)
        if rel["method"] == "none":
            logger.info(f"Gitter: {rel['reason']} (Versatz {rel['col_off']} / {rel['row_off']} Pixel)")
            col_off, row_off, alt_src = rel["col_off"], rel["row_off"], alt
        else:
            logger.warning(f"RESAMPLING: DSM alt liegt nicht im Gitter von DSM aktuell "
                           f"({rel['reason']}) - gdal.Warp {rel['method']} auf das Gitter von "
                           f"DSM aktuell. Die Werte von DSM alt sind damit interpoliert.")
            warped = warp_to_grid(alt, neu, run_dir / "dsm_alt_warped.tif", rel["method"],
                                  cancel, progress, 40, 50)
            alt_src = dict(alt, path=warped, nodata=DIFF_NODATA, gt=neu["gt"],
                           width=neu["width"], height=neu["height"])
            col_off = row_off = 0
            alt_sentinel = None
        result["resampling"] = rel["method"]

        logger.info("Differenz dh = DSM aktuell - DSM alt (negativ = Abtrag/Verlust), "
                    f"Bloecke {int(block_size)} x {int(block_size)} px")
        staged = run_dir / "dsm_diff_float32.tif"
        stats = compute_diff(neu, alt_src, col_off, row_off, staged, block_size, cancel,
                             progress, 50, 80, alt_sentinel)
        result["stats"] = stats
        _log_stats(stats)
        if stats["valid"] == 0:
            raise ValueError("Kein Pixel hat in beiden DSM einen Wert - keine Ueberlappung "
                             "der gueltigen Daten.")

        items = _diff_metadata(neu, alt, alt_quelle, alt_desc, rel, s3d_result, stamp, metadata)
        _set_diff_metadata(staged, items, stats)
        logger.info("COG schreiben (DEFLATE, PREDICTOR=FLOATING_POINT, Bloecke 512, Overviews AVERAGE)")
        write_cog(staged, tmp_cog, cancel, progress, 80, 97)
        problems = check_cog(tmp_cog, neu["width"], neu["height"], neu["gt"], vertical=None,
                             require_metadata=("DSMDIFF_DEFINITION", "DSMDIFF_SOURCE_NEW",
                                               "DSMDIFF_SOURCE_OLD", "DSMDIFF_CREATED",
                                               "TIFFTAG_DATETIME"),
                             require_stats=True)
        if problems:
            raise RuntimeError("COG-Pruefung fehlgeschlagen: " + "; ".join(problems))
        logger.info("COG-Pruefung OK: Layout COG, DEFLATE, Predictor 3, NoData bitgenau, Gitter "
                    "= DSM aktuell, EPSG:2056, Einheit metre, Metadaten, keine aux.xml")
        _check_cancel(cancel, "vor dem Ablegen")

        if tmp_mosaic:
            _place(tmp_mosaic, mosaic_out)
            result["mosaic"] = str(mosaic_out)
        _place(tmp_cog, out_diff_cog)
        if progress:
            progress(100, "fertig")
        result.update(neu_name=neu["name"], alt_desc=alt_desc, gsd=neu["res_x"], bounds=neu["bounds"],
                      height_ref_neu=neu.get("height_ref"), height_ref_alt=alt.get("height_ref"),
                      metadata=items)
        return result
    finally:
        _remove(tmp_cog)
        _remove(tmp_mosaic)
        if keep_staging:
            logger.info(f"Staging bleibt (Option 'Staging behalten'): {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)


def _place(tmp, target):
    """Fertige, gepruefte Datei an den Zielort. Eine alte aux.xml des Ziels gehoert zur
    alten Datei und wuerde deren Metadaten vortaeuschen - sie wird entfernt."""
    os.replace(tmp, target)
    aux = str(target) + ".aux.xml"
    if os.path.exists(aux):
        _remove(aux)
        logger.warning(f"Alte aux.xml entfernt (gehoerte zur ueberschriebenen Datei): {aux}")
    logger.info(f"Geschrieben: {target}  ({os.path.getsize(target) / 1048576:.1f} MB)")


def _prepare_swisssurface3d(neu, found, session, staging_dir, run_dir, tmp_mosaic, cancel,
                            progress):
    """Download (Cache), Pruefung der Kacheln, VRT und Mosaik-COG (Merged TIFF)."""
    if session is None:
        from _stac_http import get_session
        session = get_session()
    cache = Path(staging_dir) / s3d.CACHE_DIRNAME

    def dl_progress(frac, text):
        if progress:
            progress(2 + 28 * frac, f"Download swissSURFACE3D: {text}")

    s3d.check_height_reference(found["chosen"])        # nochmals, falls das Ergebnis von aussen kommt
    paths = s3d.download_tiles(session, found["chosen"], cache, cancel, dl_progress)
    tiles = [raster_facts(paths[k], f"swissSURFACE3D {k}") for k in sorted(paths)]
    first = tiles[0]
    # Stichprobe CRS-Tag (alle Kacheln haben dasselbe CRS): traegt die Kachel einen
    # Hoehenbezug, muss er LN02 sein; ein Widerspruch Tag/Assetname bricht ab.
    epsg, _ = resolve_height_ref(first)
    if epsg != s3d.S3D_VERTICAL_EPSG:
        raise ValueError(f"swissSURFACE3D-Kachel {first['name']}: Hoehenbezug "
                         f"{height_ref_label(epsg)} statt LN02 (EPSG:5728) - Abbruch.")
    logger.info(f"  Kachel {first['name']}: CRS-Tag {first['srs_name']}"
                + ("" if first["vertical_tag"] else " (Hoehenbezug nicht getaggt)")
                + " - LN02 laut STAC-Assetname _2056_5728")
    for t in tiles[1:]:
        if (t["res_x"], t["res_y"], t["nodata"]) != (first["res_x"], first["res_y"], first["nodata"]):
            raise ValueError(f"swissSURFACE3D-Kacheln uneinheitlich: {t['name']} hat Zelle "
                             f"{t['res_x']:g} m / NoData {t['nodata']!r}, {first['name']} "
                             f"{first['res_x']:g} m / {first['nodata']!r}")
    res = first["res_x"]
    bounds = s3d.snapped_bounds(neu["bounds"], res)
    vrt = s3d.build_vrt([t["path"] for t in tiles], bounds, res, first["nodata"],
                        DIFF_NODATA, run_dir / "swisssurface3d.vrt")
    years = found["years"]
    tiles_txt = ",".join(f"{k}:{v['year']}" for k, v in sorted(found["chosen"].items()))
    md = {
        "S3D_MOSAIC_SOURCE": f"swissSURFACE3D Raster, {s3d.S3D_API}/collections/{s3d.S3D_COLLECTION}",
        "S3D_MOSAIC_YEARS": ",".join(str(y) for y in years),
        "S3D_MOSAIC_TILES": tiles_txt,
        "S3D_MOSAIC_YEAR_RULE": (f"latest year {'<=' if found['allow_same_year'] else '<'} "
                                 f"{found['ref_year']} per tile"),
        "S3D_MOSAIC_NODATA": f"source NoData {first['nodata']!r} set to {DIFF_NODATA!r}",
        "S3D_MOSAIC_HEIGHT_REF": "LN02 (EPSG:5728) per product definition (asset name _2056_5728)",
        "S3D_MOSAIC_CREATED": datetime.datetime.now().isoformat(timespec="seconds"),
        "TIFFTAG_DATETIME": datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S"),
    }
    if found["missing"] or found["no_suitable_year"]:
        md["S3D_MOSAIC_MISSING_TILES"] = ",".join(found["missing"] + sorted(found["no_suitable_year"]))
    vds = gdal.Open(vrt, gdal.GA_Update)
    vds.SetMetadata(md)
    vds.GetRasterBand(1).SetDescription("swissSURFACE3D Raster mosaic [m, LN02]")
    vds = None
    logger.info(f"Mosaik swissSURFACE3D ({len(tiles)} Kachel(n), Jahre {md['S3D_MOSAIC_YEARS']}, "
                f"Zelle {res:g} m, NoData {first['nodata']!r} -> {DIFF_NODATA!r}, CRS {s3d.S3D_SRS})")
    write_cog(vrt, tmp_mosaic, cancel, progress, 30, 40, output_srs=s3d.S3D_SRS,
              text="Mosaik swissSURFACE3D schreiben")
    vds = gdal.Open(vrt)
    w, h, gt = vds.RasterXSize, vds.RasterYSize, tuple(vds.GetGeoTransform())
    vds = None
    problems = check_cog(tmp_mosaic, w, h, gt, vertical=s3d.S3D_VERTICAL_EPSG,
                         require_metadata=("S3D_MOSAIC_YEARS", "S3D_MOSAIC_TILES"))
    if problems:
        raise RuntimeError("Pruefung Mosaik fehlgeschlagen: " + "; ".join(problems))
    alt = raster_facts(tmp_mosaic, "DSM alt (swissSURFACE3D-Mosaik)")
    alt["source_nodata"] = first["nodata"]
    log_facts(alt)
    desc = (f"swissSURFACE3D Raster ({s3d.S3D_COLLECTION}), years {md['S3D_MOSAIC_YEARS']}, "
            f"{len(tiles)} tile(s)")
    return alt, desc


def _diff_metadata(neu, alt, alt_quelle, alt_desc, rel, found, stamp, extra):
    def ref(f):
        how = {"tag": "CRS tag", "stac": "STAC description", "name": "file name only"}.get(
            f.get("height_ref_source"), "not tagged")
        return f"{height_ref_label(f.get('height_ref'))}, {how}" if f.get("height_ref") else how
    items = {
        "DSMDIFF_DEFINITION": "dh = DSM new - DSM old [metre]; negative = loss / removal",
        "DSMDIFF_SOURCE_NEW": neu["name"],
        "DSMDIFF_SOURCE_OLD": alt_desc,
        "DSMDIFF_SOURCE_OLD_TYPE": "swissSURFACE3D" if alt_quelle == ALT_S3D else "file",
        "DSMDIFF_HEIGHT_REF_NEW": ref(neu),
        "DSMDIFF_HEIGHT_REF_OLD": ref(alt),
        "DSMDIFF_RESAMPLING": ("none (same grid)" if rel["method"] == "none"
                               else f"{rel['method']} ({rel['reason']})"),
        "DSMDIFF_CREATED": stamp.isoformat(timespec="seconds"),
        "DSMDIFF_SOFTWARE": f"topo-rmDSMdiff dsm_diff.py, GDAL {gdal.__version__}",
        "TIFFTAG_DATETIME": stamp.strftime("%Y:%m:%d %H:%M:%S"),
        "TIFFTAG_IMAGEDESCRIPTION": "DSM difference dh = DSM new - DSM old (metre, negative = loss)",
    }
    if neu.get("source_url"):
        items["DSMDIFF_SOURCE_NEW_HREF"] = neu["source_url"]
    if alt_quelle == ALT_S3D and found:
        items["DSMDIFF_SOURCE_OLD_YEARS"] = ",".join(str(y) for y in found["years"])
        items["DSMDIFF_SOURCE_OLD_TILES"] = ",".join(
            f"{k}:{v['year']}" for k, v in sorted(found["chosen"].items()))
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            items[k] = v
    return items


def _log_stats(s):
    logger.info(f"Statistik dh: gueltig {s['valid']} von {s['total']} Pixel "
                f"({s['valid_percent']:.2f} %)")
    if s["valid"]:
        logger.info(f"  min {s['min']:.3f} m, max {s['max']:.3f} m, mittel {s['mean']:.3f} m, "
                    f"std {s['std']:.3f} m")
    logger.info(f"  Flaeche von DSM aktuell ohne Referenz: {s['no_reference']} Pixel "
                f"({s['no_reference_percent']:.2f} % der gueltigen Pixel von DSM aktuell)")
    if s["no_reference_percent"] > 0:
        logger.warning(f"{s['no_reference_percent']:.2f} % der Flaeche von DSM aktuell haben "
                       f"keine Referenz - das Diff ist dort NoData.")
    if s["huge_values_masked"]:
        logger.warning(f"{s['huge_values_masked']} Pixel mit |Wert| > {HUGE_ABS:g} ohne "
                       f"NoData-Tag als NoData behandelt.")


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(description="dh = DSM aktuell - DSM alt als COG (ohne GUI).")
    p.add_argument("dsm_neu", help="DSM aktuell (GeoTIFF/COG, Pfad oder STAC-Link)")
    p.add_argument("out", help="Ausgabe DIFF-COG (.tif)")
    p.add_argument("--alt", help="eigenes DSM alt (Pfad oder STAC-Link); ohne Angabe "
                                 "swissSURFACE3D aus dem STAC")
    p.add_argument("--ref-year", type=int, help="Aufnahmejahr (fuer swissSURFACE3D)")
    p.add_argument("--mosaic", help="Ausgabe swissSURFACE3D-Mosaik (.tif)")
    p.add_argument("--secrets-dir", help="Ordner mit proxy_config.json (Netzzugriff)")
    p.add_argument("--staging", help="Staging-Ordner (Default: Temp)")
    p.add_argument("--keep-staging", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--block-size", type=int, default=2048)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    session = None
    if not a.alt or _is_url(a.dsm_neu) or _is_url(a.alt):
        from _stac_http import get_session, PROXY_FILE
        session = get_session(Path(a.secrets_dir) / PROXY_FILE if a.secrets_dir else None)
    try:
        run_dsm_diff(a.dsm_neu, a.out, dsm_alt=a.alt, alt_quelle=ALT_FILE if a.alt else ALT_S3D,
                     staging_dir=a.staging, overwrite=a.overwrite, block_size=a.block_size,
                     ref_year=a.ref_year, mosaic_out=a.mosaic, session=session,
                     keep_staging=a.keep_staging)
    except Exception as e:
        logger.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
