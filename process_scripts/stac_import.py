"""
stac_import.py - STAC-Namen, Asset-Beschreibung und Import des DSM-DIFF als Asset.

Teil 1 - nur Standardbibliothek, auch vom GUI fuer die Live-Vorschau genutzt:
    item_id, asset_name, browser_link, acquisition_times, build_description, ...
Teil 2 - Netzwerk (requests ueber _stac_http, erst beim Aufruf geladen):
    check_item, put_asset, verify_asset, run_stac_import

Grundregeln (siehe .claude/CLAUDE.md):
  - Ein BESTEHENDES Item wird NIE geschrieben (kein PUT/PATCH) - es gehoert dem GDWH-Import
    (DSM, Hillshade, DOP, Punktwolke). publish_to_stac aus rapidmapping wuerde es mit neuer
    Geometrie und neuen Links ueberschreiben.
  - FEHLT das Item (das DIFF kann vor DSM-RASTER und DOP in STAC kommen), wird es angelegt:
    unmittelbar vorher nochmals per GET geprueft, dann PUT mit id, Geometrie = Perimeter von
    DSM aktuell (WGS84) und properties.datetime = Linienstart der ersten LineID. Die API
    kennt kein reines "Anlegen" (PUT = anlegen oder aktualisieren, 201 bzw. 200) - deshalb
    die zweite Pruefung und die Kontrolle des Statuscodes.
  - STAC-Namen sind eine externe Schnittstelle: Item <auftragstyp>-<StacItemIdDatetime>,
    Asset <item>-dsm-diff.tif (klein geschrieben), Titel DSM-DIFF.
  - INT ist immer Default.
"""

import logging
import math
import re

import _line_ids

logger = logging.getLogger(__name__)

STAC_COLLECTION = "ch.swisstopo.spezialbefliegungen"
STAC_HOSTS = {"INT": "sys-data.int.bgdi.ch", "PROD": "data.geo.admin.ch"}
STAC_API_PATH = "/api/stac/v0.9"
DEFAULT_ENV = "INT"

ASSET_SUFFIX = "-dsm-diff.tif"
ASSET_TITLE = "DSM-DIFF"
REF_ASSET_SUFFIX = "-dsm-raster.tif"      # DSM-RASTER desselben Items (Vergleich Area/GSD)
ASSET_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
ASSET_EPSG = 2056
ASSET_UNIT = "metre"                      # Einheit der dh-Werte, fest in der Beschreibung (Entscheid Nutzer 2026-10-26)
# Hoehengenauigkeit 1 sigma je DSM in Meter (Entscheid Nutzer 2026-10-26): Spezifikation der
# photogrammetrischen DSM-Raster; swissSURFACE3D = 50-cm-Raster aus neuen LiDAR-Daten +/- 30 cm
# laut swissALTI3D-Produktinfo (Maerz 2022) Kap. 1.4 - die +/- 10 cm (Kap. 3.2.2, Produktseite
# swissSURFACE3D Raster) gelten fuer die LiDAR-Basisdaten, nicht fuers Raster
DEFAULT_SIGMA = "0.5-1"
DEFAULT_SIGMA_S3D = "0.3"
LOD95_FACTOR = 1.96                       # Nachweisgrenze 95 % = 1.96 * sigma_dh
GSD_TOLERANCE_M = 0.001

# Auswahllisten wie topo-GDWHimport/GUI_GDWHimport.py (Stand 2026-10-20)
AUFTRAGSTYPEN = {
    "kry": "Kryosphäre (GLAMOS)",
    "ram": "Rapid Mapping",
    "bim": "Biotop Monitoring",
    "mom": "Moor Monitoring",
    "wam": "Wald Monitoring",
}
DEFAULT_AUFTRAGSTYP = "ram"
TERRAIN_MODELS = [
    "Digital Surface Model (DSM photogrammetric autocorrelation)",
    "swissALTI3D", "swissALTI3D/DHM25", "swissSURFACE3D",
]
CAMERA_SYSTEMS = ["Leica ADS100", "Leica ADS80", "Leica DMC-4"]
DEFAULT_CAMERA = _line_ids.DMC_CAMERA
# Nur Werte aus dem GDWH-Wertebereich (siehe GUI_GDWHimport.SOURCE_REF_SYS)
SOURCE_REF_SYS = "(EPSG:2056) CH1903+ / LV95_LN02"
TERRAIN_MODEL_1_DEFAULT = TERRAIN_MODELS[0]
TERRAIN_MODEL_S3D = "swissSURFACE3D"
# Kurznamen in der DIFF-Beschreibung, '<JAHR>-<Kurzname>' (Entscheid Nutzer 2026-10-20)
TERRAIN_MODEL_SHORT = {TERRAIN_MODEL_1_DEFAULT: "DSM-photogrammetric",
                       TERRAIN_MODEL_S3D: "swissSURFACE3D-DSM"}
DEFAULT_COMMENTARY = ("Digital Surface Model - Difference "
                      "(TerrainModel 1 minus TerrainModel 2, negative = loss)")


# ─── Teil 1: Namen und Beschreibung (Standardbibliothek) ──────────────────────
def normalized_line_ids(line_ids, camera):
    """Geprueft, ohne Duplikate, chronologisch (_line_ids.normalize). ValueError sonst."""
    ids = [str(l).strip() for l in line_ids if str(l).strip()]
    if not ids:
        raise ValueError("Keine LineIDs angegeben.")
    return _line_ids.normalize(ids, camera)


def stac_datetime(line_ids, camera):
    """StacItemIdDatetime der chronologisch ersten Linie, z.B. 2026-08-13t08222100."""
    return _line_ids.stac_datetime(normalized_line_ids(line_ids, camera), camera)


def item_id(auftragstyp, line_ids, camera):
    """<auftragstyp>-<StacItemIdDatetime>, z.B. kry-2026-08-13t08222100."""
    code = str(auftragstyp).strip().lower()
    if code not in AUFTRAGSTYPEN:
        raise ValueError(f"Unbekannter Auftragstyp {auftragstyp!r} "
                         f"(erlaubt: {', '.join(AUFTRAGSTYPEN)})")
    dt = stac_datetime(line_ids, camera)
    if dt == "—":
        raise ValueError("Aus den LineIDs laesst sich keine StacItemIdDatetime ableiten.")
    return f"{code}-{dt}".lower()


def asset_name(item):
    """Asset-Name, klein geschrieben (FSDI verlangt das) - unabhaengig vom Dateinamen."""
    return f"{item}{ASSET_SUFFIX}".lower()


def _host(env):
    env = str(env).upper()
    if env not in STAC_HOSTS:
        raise ValueError(f"Umgebung {env!r} unbekannt (INT oder PROD)")
    return STAC_HOSTS[env]


def api_base(env):
    return f"https://{_host(env)}{STAC_API_PATH}"


def item_url(env, item):
    return f"{api_base(env)}/collections/{STAC_COLLECTION}/items/{item}"


def asset_url(env, item, asset):
    return f"{item_url(env, item)}/assets/{asset}"


def uploads_url(env, item, asset):
    return f"{asset_url(env, item, asset)}/uploads"


def browser_link(item, env=DEFAULT_ENV):
    return (f"https://{_host(env)}/browser/index.html#/collections/"
            f"{STAC_COLLECTION}/items/{item}")


def acquisition_times(line_ids, camera):
    """Aufnahmezeiten wie im GDWH-XML (AcquisitionTimes): YYYY-MM-DDTHH:MM:SS.00.
    DMC-4: Linienstart jeder Linie (Sekunden, Hundertstel 00). ADS: HHMM, Sekunden 00."""
    ids = normalized_line_ids(line_ids, camera)
    if _line_ids.is_dmc(camera):
        out = []
        for l in ids:
            t = _line_ids.dmc_acquisition(l)
            out.append(f"{t['year']:04d}-{t['month']:02d}-{t['day']:02d}T{t['hh']:02d}:"
                       f"{t['mm']:02d}:{t['ss']:02d}.{t['hundredths']:02d}")
        return out
    return [f"{l[0:4]}-{l[4:6]}-{l[6:8]}T{l[9:11]}:{l[11:13]}:00.00" for l in ids]


def terrain_model_label(model, years=None, link=None):
    """TerrainModel 1/2 der DIFF-Beschreibung: '<JAHR>-<Kurzname>', mehrere Jahre mit '/',
    ein STAC-Link mit seinem Asset-Namen (Entscheid Nutzer: reicht zum Nachvollziehen); ein
    lokaler Pfad wird nicht genannt. Z.B. '2021/2023-swissSURFACE3D-DSM',
    '2026-DSM-photogrammetric (kry-2026-08-13t08222100-dsm-raster.tif)'."""
    model = str(model or "").strip()
    if not model:
        return ""
    name = TERRAIN_MODEL_SHORT.get(model, model)
    ys = sorted({str(y).strip() for y in (years or []) if str(y).strip()})
    label = f"{'/'.join(ys)}-{name}" if ys else name
    return f"{label} ({parse_asset_href(link)['asset']})" if link else label


def parse_sigma(text):
    """Hoehengenauigkeit 1 sigma in Meter: '0.5-1', '0,5–1' oder '0.5' -> (0.5, 1.0) bzw.
    (0.5, 0.5). ValueError, wenn keine positive Zahl oder Untergrenze > Obergrenze."""
    t = str(text or "").strip().replace(",", ".").replace("–", "-")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", t)
    if not m:
        raise ValueError(f"Hoehengenauigkeit {text!r}: Zahl oder Bereich in Meter erwartet, "
                         f"z.B. {DEFAULT_SIGMA}")
    lo, hi = float(m.group(1)), float(m.group(2) or m.group(1))
    if lo <= 0 or hi < lo:
        raise ValueError(f"Hoehengenauigkeit {text!r}: positiv und Untergrenze <= Obergrenze "
                         f"erwartet")
    return lo, hi


def _range_text(lo, hi, fmt):
    lo, hi = fmt.format(lo), fmt.format(hi)
    return lo if lo == hi else f"{lo}-{hi}"


def sigma_text(text):
    """Eingabe normalisiert fuer die DSMDIFF-Metadaten, z.B. '0,5–1' -> '0.5-1'."""
    return _range_text(*parse_sigma(text), "{:g}")


def vertical_accuracy(sigma_neu, sigma_alt):
    """VerticalAccuracy der DIFF-Beschreibung (Entscheid Nutzer 2026-10-26). Die Fehler der
    beiden Befliegungen gelten als unabhaengig: sigma_dh = sqrt(s1² + s2²), LoD95 = 1.96 *
    sigma_dh; Bereiche je fuer Unter- und Obergrenze. Z.B. '0.5-1' und '0.5-1' ->
    '0.71-1.41 (1 sigma; LoD95 1.39-2.77)'."""
    (lo1, hi1), (lo2, hi2) = parse_sigma(sigma_neu), parse_sigma(sigma_alt)
    lo, hi = math.hypot(lo1, lo2), math.hypot(hi1, hi2)
    return (f"{_range_text(lo, hi, '{:.2f}')} (1 sigma; LoD95 "
            f"{_range_text(LOD95_FACTOR * lo, LOD95_FACTOR * hi, '{:.2f}')})")


def build_description(area, terrain_models, camera, line_ids, commentary,
                      source_ref_sys=SOURCE_REF_SYS, unit=ASSET_UNIT, accuracy=None):
    """Asset-Beschreibung als Text im Muster der GDWH-Assets (kein JSON).

    Reihenfolge und Schreibweise wie beim DSM-RASTER-Asset. Ein Terrainmodell ergibt
    'TerrainModel: ...' (so baut der GDWH die bestehenden Assets), zwei ergeben
    'TerrainModel 1: ..., TerrainModel 2: ...' (Entscheid 3). Acquisition time und LineId
    sortiert, kommagetrennt ohne Leerzeichen. 'Unit: metre' und 'VerticalAccuracy: ...'
    (vertical_accuracy) nach SourceReferenceSystem gibt es nur im DIFF (unit=None,
    accuracy=None ergibt das reine GDWH-Muster)."""
    if isinstance(terrain_models, str):
        terrain_models = [terrain_models]
    if not str(area or "").strip():
        raise ValueError("Area fehlt.")
    if not terrain_models or any(not str(t or "").strip() for t in terrain_models):
        raise ValueError("TerrainModel fehlt.")
    ids = normalized_line_ids(line_ids, camera)
    parts = [f"Area: {str(area).strip()}"]
    if len(terrain_models) == 1:
        parts.append(f"TerrainModel: {terrain_models[0]}")
    else:
        parts += [f"TerrainModel {i}: {t}" for i, t in enumerate(terrain_models, 1)]
    parts.append(f"SourceReferenceSystem: {source_ref_sys}")
    if unit:
        parts.append(f"Unit: {unit}")
    if accuracy:
        parts.append(f"VerticalAccuracy: {accuracy}")
    parts += [f"CameraSystem: {camera}",
              f"Acquisition time: {','.join(acquisition_times(ids, camera))}",
              f"LineId: {','.join(ids)}",
              f"Commentary: {commentary}"]
    return ", ".join(parts)


def area_from_description(description):
    """'Area: GUPPENFIRN, ...' -> 'GUPPENFIRN' (None, wenn nicht vorhanden)."""
    m = re.match(r"\s*Area:\s*([^,]*)", description or "")
    return (m.group(1).strip() or None) if m else None


def asset_payload(asset, gsd, description):
    return {"id": asset, "title": ASSET_TITLE, "type": ASSET_MEDIA_TYPE,
            "proj:epsg": ASSET_EPSG, "eo:gsd": float(gsd), "description": description}


# ─── STAC-Link als Eingabe (DSM aktuell / DSM alt) ────────────────────────────
# Daten-href: https://<host>/<collection>/<item>/<asset>.tif
# API-URL   : https://<host>/api/stac/v0.9/collections/<c>/items/<i>/assets/<a>
_API_HREF = re.compile(r"^https?://(?P<host>[^/]+)/api/stac/v0\.9/collections/(?P<collection>[^/]+)"
                       r"/items/(?P<item>[^/]+)/assets/(?P<asset>[^/?#]+)/?$", re.IGNORECASE)
_DATA_HREF = re.compile(r"^https?://(?P<host>[^/]+)/(?P<collection>[^/]+)/(?P<item>[^/]+)"
                        r"/(?P<asset>[^/?#]+)$", re.IGNORECASE)
# Schluessel der GDWH-Asset-Beschreibung (laengere zuerst: 'TerrainModel 1' vor 'TerrainModel')
DESCRIPTION_KEYS = ("Area", "TerrainModel 1", "TerrainModel 2", "TerrainModel",
                    "SourceReferenceSystem", "Unit", "VerticalAccuracy", "CameraSystem",
                    "Acquisition time", "LineId", "Commentary")
_DESC_KEY_PATTERN = re.compile(r"(?:^|, )(" + "|".join(re.escape(k) for k in DESCRIPTION_KEYS)
                               + r"): ")


def is_url(text):
    return str(text or "").strip().strip('"').lower().startswith(("http://", "https://"))


def parse_asset_href(url):
    """{'env', 'host', 'collection', 'item', 'asset', 'url'} aus einem STAC-Link. ValueError,
    wenn der Link nicht zu INT/PROD gehoert oder kein GeoTIFF-Asset ist."""
    url = str(url or "").strip().strip('"')
    m = _API_HREF.match(url) or _DATA_HREF.match(url)
    if not m:
        raise ValueError(f"STAC-Link nicht erkannt: {url}\n  erwartet https://<host>/<collection>/"
                         f"<item>/<asset>.tif (href des Assets) oder die API-URL des Assets")
    host = m.group("host").lower()
    env = next((e for e, h in STAC_HOSTS.items() if h == host), None)
    if env is None:
        raise ValueError(f"STAC-Host {host} unbekannt - erlaubt: "
                         + ", ".join(f"{h} ({e})" for e, h in STAC_HOSTS.items()))
    asset = m.group("asset")
    if not asset.lower().endswith((".tif", ".tiff")):
        raise ValueError(f"STAC-Asset {asset}: GeoTIFF/COG (.tif) erwartet")
    return {"env": env, "host": host, "collection": m.group("collection"),
            "item": m.group("item"), "asset": asset, "url": url}



def description_fields(description):
    """GDWH-Asset-Beschreibung -> {'Area': ..., 'LineId': ..., ...} (erste Nennung zaehlt).
    Werte duerfen Kommas enthalten (LineId, Acquisition time) - getrennt wird nur an
    ', <bekannter Schluessel>: '."""
    text = description or ""
    hits = list(_DESC_KEY_PATTERN.finditer(text))
    out = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.setdefault(m.group(1), text[m.end():end].strip())
    return out


def height_ref_from_source_ref_sys(text):
    """'(EPSG:2056) CH1903+ / LV95_LN02' -> 5728, '..._LHN95' -> 5729, sonst None."""
    t = str(text or "").upper()
    if "LHN95" in t:
        return 5729
    if "LN02" in t:
        return 5728
    return None


def auftragstyp_from_item(item):
    code = str(item or "").split("-", 1)[0].lower()
    return code if code in AUFTRAGSTYPEN else None


def asset_summary(ref, asset):
    """Was das GUI zum Vorbelegen und der Runner zum Laden braucht, aus dem Asset-JSON."""
    desc = asset.get("description") or ""
    f = description_fields(desc)
    line_ids = [l.strip() for l in f.get("LineId", "").split(",") if l.strip()]
    times = [t.strip() for t in f.get("Acquisition time", "").split(",") if t.strip()]
    year = None
    for candidate in ([line_ids[0][:4]] if line_ids else []) + ([times[0][:4]] if times else []):
        if re.fullmatch(r"(19|20)\d{2}", candidate):
            year = int(candidate)
            break
    return {"env": ref["env"], "collection": ref["collection"], "item": ref["item"],
            "asset": ref["asset"], "url": ref["url"], "href": asset.get("href") or ref["url"],
            "checksum": asset.get("checksum:multihash", ""), "gsd": asset.get("eo:gsd"),
            "epsg": asset.get("proj:epsg"), "title": asset.get("title"), "description": desc,
            "area": f.get("Area"), "camera": f.get("CameraSystem"), "line_ids": line_ids,
            "terrain_model": f.get("TerrainModel") or f.get("TerrainModel 1"),
            "source_ref_sys": f.get("SourceReferenceSystem"),
            "height_ref": height_ref_from_source_ref_sys(f.get("SourceReferenceSystem")),
            "auftragstyp": auftragstyp_from_item(ref["item"]), "year": year}


def item_datetime(line_ids, camera):
    """properties.datetime eines Items wie beim GDWH-Import: Linienstart der ersten LineID,
    z.B. 2026-08-13T08:22:21Z."""
    return acquisition_times(line_ids, camera)[0][:19] + "Z"


def item_payload(item, line_ids, camera, polygon_wgs84):
    """Neues Item: nur id, Geometrie und datetime (die GDWH-Items tragen auch keinen Titel)."""
    return {"id": item, "geometry": {"type": "Polygon", "coordinates": [polygon_wgs84]},
            "properties": {"datetime": item_datetime(line_ids, camera)}}


def lv95_bounds_to_wgs84_polygon(bounds):
    """Rechteck (minx, miny, maxx, maxy) in LV95 -> geschlossener Ring [lon, lat] (WGS84).
    osr erst hier - Teil 1 dieses Moduls bleibt reine Standardbibliothek."""
    from osgeo import osr
    osr.UseExceptions()
    src = osr.SpatialReference()
    src.ImportFromEPSG(2056)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    minx, miny, maxx, maxy = bounds
    ring = []
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)):
        lon, lat = tr.TransformPoint(x, y)[:2]
        ring.append([round(lon, 7), round(lat, 7)])
    return ring


# ─── Teil 2: Netzwerk ─────────────────────────────────────────────────────────
def _response_text(r):
    try:
        return str(r.json().get("description", ""))[:300]
    except Exception:
        return ""


def fetch_asset(session, url):
    """Metadaten eines STAC-Assets (GET .../assets/<asset>) als asset_summary, dazu die
    Dateigroesse aus HEAD (size_mb, falls der Server sie nennt)."""
    ref = parse_asset_href(url)
    api = (f"https://{ref['host']}{STAC_API_PATH}/collections/{ref['collection']}/items/"
           f"{ref['item']}/assets/{ref['asset']}")
    r = session.get(api)
    if r.status_code == 404:
        raise ValueError(f"STAC-Asset {ref['asset']} in Item {ref['item']} ({ref['env']}) "
                         f"nicht gefunden.")
    if r.status_code != 200:
        raise RuntimeError(f"STAC-Asset {ref['asset']} nicht lesbar: HTTP {r.status_code} "
                           f"{_response_text(r)}")
    info = asset_summary(ref, r.json())
    info["size_mb"] = None
    try:
        h = session.head(info["href"], allow_redirects=True)
        if h.status_code == 200 and h.headers.get("Content-Length"):
            info["size_mb"] = round(int(h.headers["Content-Length"]) / 1048576, 1)
    except Exception:
        pass
    return info


def check_gsd(check, gsd):
    """GSD-Vergleich mit dem DSM-RASTER-Asset nachholen, wenn check_item ohne GSD lief
    (Vorpruefung vor dem Lesen des Rasters). Abweichung nur Warnung (Entscheid 6)."""
    ref_gsd = check.get("ref_gsd")
    if ref_gsd is not None and gsd is not None and abs(float(ref_gsd) - float(gsd)) > GSD_TOLERANCE_M:
        msg = f"GSD weicht ab: DSM aktuell {float(gsd):g} m, DSM-RASTER {ref_gsd} m."
        logger.warning(f"Item-Pruefung: {msg}")
        check.setdefault("warnings", []).append(msg)


def check_item(session, env, item, line_ids, camera, area, gsd, replace_existing=False):
    """Vorpruefung - vor der Rechnung und nochmals direkt vor dem Upload.

    Item fehlt: kein Fehler - es wird beim Import angelegt (das DIFF kann vor DSM-RASTER
    und DOP in STAC kommen). Item vorhanden: properties.datetime muss zur ersten LineID
    passen, sonst ValueError (falsches Item). Hat es schon ein DSM-RASTER-Asset, werden
    dessen eo:gsd und Area verglichen - Abweichung nur Warnung (Entscheid 6). DIFF-Asset
    schon da ohne 'ersetzen' -> ValueError. gsd None: GSD-Vergleich spaeter (check_gsd).
    Gibt {'item_exists', 'diff_exists', 'ref_gsd', 'warnings'} zurueck."""
    env = str(env).upper()
    r = session.get(item_url(env, item))
    if r.status_code == 404:
        logger.info(f"Item {item} gibt es in {env} noch nicht - es wird beim Import angelegt "
                    f"(Geometrie = Perimeter DSM aktuell, datetime {item_datetime(line_ids, camera)}).")
        return {"item_exists": False, "diff_exists": False, "ref_gsd": None, "warnings": []}
    if r.status_code != 200:
        raise RuntimeError(f"Item {item} ({env}) nicht lesbar: HTTP {r.status_code} "
                           f"{_response_text(r)}")
    data = r.json()
    warnings = []

    first = acquisition_times(line_ids, camera)[0][:19]          # YYYY-MM-DDTHH:MM:SS
    got = str((data.get("properties") or {}).get("datetime") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", got)
    if not m or m.group(1) != first:
        raise ValueError(f"Item {item} ({env}): properties.datetime {got or '(leer)'} passt nicht "
                         f"zur ersten LineID ({first}) - falsches Item?")

    assets = data.get("assets") or {}
    ref_name = f"{item}{REF_ASSET_SUFFIX}"
    ref = assets.get(ref_name)
    ref_gsd = None
    if ref is None:
        logger.info(f"Item {item} hat (noch) kein Asset {ref_name} - Area/GSD nicht verglichen.")
    else:
        ref_gsd = ref.get("eo:gsd")
        if ref_gsd is None:
            warnings.append(f"{ref_name} nennt kein eo:gsd - GSD nicht verglichen.")
        elif gsd is not None and abs(float(ref_gsd) - float(gsd)) > GSD_TOLERANCE_M:
            warnings.append(f"GSD weicht ab: DSM aktuell {float(gsd):g} m, {ref_name} {ref_gsd} m.")
        ref_area = area_from_description(ref.get("description"))
        if ref_area is None:
            warnings.append(f"{ref_name} nennt keine Area - Area nicht verglichen.")
        elif ref_area.casefold() != str(area).strip().casefold():
            warnings.append(f"Area weicht ab: Eingabe {area!r}, {ref_name} {ref_area!r}.")
    for w in warnings:
        logger.warning(f"Item-Pruefung: {w}")

    asset = asset_name(item)
    exists = asset in assets
    if exists and not replace_existing:
        raise ValueError(f"Asset {asset} existiert in {env} bereits. Ersetzen nur mit Haken "
                         f"'vorhandenes STAC-Asset ersetzen'.\n  {browser_link(item, env)}")
    if exists:
        logger.warning(f"Asset {asset} existiert in {env} bereits und wird ersetzt.")
    logger.info(f"Item-Pruefung OK: {item} ({env}) vorhanden, datetime {got}, "
                f"{len(assets)} Asset(s) - das Item selbst wird nicht veraendert.")
    return {"item_exists": True, "diff_exists": exists, "ref_gsd": ref_gsd, "warnings": warnings}


def create_item(session, env, item, payload, auth):
    """Legt ein FEHLENDES Item an. Unmittelbar vorher nochmals GET: gibt es das Item
    inzwischen (z.B. GDWH-Import parallel), wird nichts geschrieben -> False.
    Antwortet der PUT mit 200 statt 201, hat er ein inzwischen entstandenes Item
    aktualisiert - dann Abbruch mit deutlicher Meldung."""
    url = item_url(env, item)
    r = session.get(url)
    if r.status_code == 200:
        logger.warning(f"Item {item} ist inzwischen in {env} vorhanden - wird nicht angelegt.")
        return False
    if r.status_code != 404:
        raise RuntimeError(f"Item {item} ({env}) nicht pruefbar: HTTP {r.status_code}")
    r = session.put(url, json=payload, auth=auth)
    if r.status_code == 201:
        logger.info(f"Item {item} in {env} angelegt (datetime {payload['properties']['datetime']}).")
        return True
    if r.status_code == 200:
        raise RuntimeError(f"Item {item} entstand zwischen Pruefung und Anlegen und wurde durch den "
                           f"PUT aktualisiert - bitte das Item in {env} kontrollieren "
                           f"({browser_link(item, env)}).")
    raise RuntimeError(f"Item {item} anlegen fehlgeschlagen: HTTP {r.status_code} "
                       f"{_response_text(r)}")


def delete_item_if_empty(session, env, item, auth):
    """Raeumt ein in diesem Lauf angelegtes Item weg, aber nur, wenn es keine Assets hat."""
    r = session.get(item_url(env, item))
    if r.status_code != 200 or (r.json().get("assets") or {}):
        return False
    return session.delete(item_url(env, item), auth=auth).status_code // 100 == 2


def put_asset(session, env, item, asset, payload, auth):
    """Legt das Asset an (PUT .../items/<item>/assets/<asset>). Nie das Item selbst."""
    url = asset_url(env, item, asset)
    if not url.endswith(f"/items/{item}/assets/{asset}"):
        raise AssertionError(f"Sicherung: unerwartete Asset-URL {url}")
    r = session.put(url, json=payload, auth=auth)
    if r.status_code // 100 != 2:
        raise RuntimeError(f"Asset {asset} anlegen fehlgeschlagen: HTTP {r.status_code} "
                           f"{_response_text(r)}")
    logger.info(f"Asset {asset} angelegt/aktualisiert (HTTP {r.status_code})")


def delete_asset(session, env, item, asset, auth):
    r = session.delete(asset_url(env, item, asset), auth=auth)
    return r.status_code // 100 == 2


def verify_asset(session, env, item, asset, checksum, attempts=5, wait_s=3):
    """GET Asset -> checksum:multihash = lokal (ohne Gross/Klein), href erreichbar."""
    import time
    from _stac_http import same_multihash

    r = session.get(asset_url(env, item, asset))
    if r.status_code != 200:
        raise RuntimeError(f"Kontrolle: Asset {asset} nicht lesbar (HTTP {r.status_code})")
    data = r.json()
    remote = data.get("checksum:multihash")
    if not same_multihash(remote, checksum):
        raise RuntimeError(f"Kontrolle: Pruefsumme im STAC {remote!r} statt lokal {checksum!r}")
    href = data.get("href")
    for i in range(attempts):
        try:
            h = session.head(href, allow_redirects=True)
            if h.status_code == 200:
                logger.info(f"Kontrolle OK: checksum:multihash = lokal, href erreichbar ({href})")
                return href
            status = f"HTTP {h.status_code}"
        except Exception as e:
            status = type(e).__name__
        if i < attempts - 1:
            time.sleep(wait_s)
    logger.warning(f"Kontrolle: Pruefsumme stimmt, aber href noch nicht erreichbar ({status}): {href}")
    return href


def run_stac_import(diff_path, env, item, description, gsd, line_ids, camera, area,
                    bounds_lv95=None, secrets_dir=None, replace_existing=False, cancel=None,
                    progress=None, session=None, upload_sessions=None, auth=None):
    """Item anlegen, falls es fehlt; Asset <item>-dsm-diff.tif anlegen, Datei hochladen,
    kontrollieren. bounds_lv95 = Perimeter von DSM aktuell (Geometrie eines neuen Items).

    session / upload_sessions / auth werden normalerweise hier erzeugt (Credentials aus
    secrets_dir, Proxy wie rapidmapping) - die Tests geben Attrappen mit."""
    env = str(env).upper()
    _host(env)
    asset = asset_name(item)
    logger.info(f"STAC-Import nach {env} ({STAC_HOSTS[env]})")
    logger.info(f"  Item  : {item}")
    logger.info(f"  Asset : {asset}  (Titel {ASSET_TITLE})")
    logger.info(f"  Beschreibung: {description}")

    if auth is None or session is None or upload_sessions is None:
        import _stac_http
        from pathlib import Path
        if auth is None:
            user, password, file_host = _stac_http.load_stac_credentials(secrets_dir, env)
            if file_host and file_host.strip().lower() != STAC_HOSTS[env]:
                logger.warning(f"stac_credentials.json nennt fuer {env} den Host {file_host!r} - "
                               f"verwendet wird {STAC_HOSTS[env]} (aus dem Code).")
            auth = (user, password)
        proxy_path = Path(secrets_dir) / _stac_http.PROXY_FILE if secrets_dir else None
        if session is None:
            session = _stac_http.get_session(proxy_path)
        if upload_sessions is None:
            upload_sessions = _stac_http.make_upload_sessions(_stac_http.get_proxy_config())
    from _stac_http import multipart_upload

    check = check_item(session, env, item, line_ids, camera, area, gsd, replace_existing)
    item_created = False
    if not check["item_exists"]:
        if bounds_lv95 is None:
            raise ValueError(f"Item {item} fehlt und zum Anlegen fehlt der Perimeter (bounds_lv95).")
        payload = item_payload(item, line_ids, camera, lv95_bounds_to_wgs84_polygon(bounds_lv95))
        item_created = create_item(session, env, item, payload, auth)
        if not item_created:                 # inzwischen entstanden: jetzt normal pruefen
            check = check_item(session, env, item, line_ids, camera, area, gsd, replace_existing)

    try:
        put_asset(session, env, item, asset, asset_payload(asset, gsd, description), auth)
        api, s3 = upload_sessions
        checksum = multipart_upload(api, s3, uploads_url(env, item, asset), str(diff_path), auth,
                                    cancel=cancel, progress=progress)
    except BaseException:
        _cleanup_failed_import(session, env, item, asset, auth, check["diff_exists"], item_created)
        raise
    href = verify_asset(session, env, item, asset, checksum)
    link = browser_link(item, env)
    logger.info(f"Browser-Link: {link}")
    return {"env": env, "item": item, "asset": asset, "checksum": checksum, "href": href,
            "browser_link": link, "warnings": check["warnings"], "item_created": item_created}


def _cleanup_failed_import(session, env, item, asset, auth, diff_existed, item_created):
    """Nach gescheitertem Asset-PUT/Upload: keine halben Eintraege im Katalog lassen.
    Ein neu angelegtes Asset (ohne Datei) wird entfernt, ein in diesem Lauf angelegtes Item
    nur, wenn es danach keine Assets hat. Ein ersetztes Asset bleibt (alte Datei)."""
    if diff_existed:
        logger.error(f"Asset {asset}: Metadaten evtl. aktualisiert, Datei-Upload fehlgeschlagen - "
                     f"im STAC liegt weiterhin die alte Datei. Import wiederholen.")
        return
    try:
        if delete_asset(session, env, item, asset, auth):
            logger.warning(f"Neu angelegtes Asset {asset} wieder entfernt (Import fehlgeschlagen).")
    except Exception:
        logger.error(f"Asset {asset} evtl. angelegt, aber ohne Datei - von Hand pruefen/entfernen "
                     f"(README, Abschnitt 'Falsch hochgeladenes Asset').")
    if item_created:
        try:
            if delete_item_if_empty(session, env, item, auth):
                logger.warning(f"In diesem Lauf angelegtes, leeres Item {item} wieder entfernt.")
                return
        except Exception:
            pass
        logger.error(f"Item {item} wurde in diesem Lauf angelegt und konnte nicht entfernt werden "
                     f"- von Hand pruefen ({browser_link(item, env)}).")
