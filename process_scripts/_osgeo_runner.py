"""
_osgeo_runner.py - wird vom GUI im OSGeo4W/QGIS-Python gestartet (NICHT direkt aufrufen).
Liest die Parameter aus einer JSON-Datei, fuehrt die Aktion aus, Log auf stdout.

Aufbau wie topo-DMCdataConverter/process_scripts/_osgeo_runner.py (Stand 2026-10-20):
JSON-Config, Aktionen-Dispatcher in main(), Fortschritt als 'PROGRESS:<0..1>' auf stdout.

Aktionen:
    info         Raster-Info einer Datei bzw. Metadaten eines STAC-Links als JSON (GUI)
    s3d_search   swissSURFACE3D-Kacheln und -Jahre im Perimeter von DSM aktuell, als JSON
    dsm_diff     Differenzraster (+ Mosaik bei swissSURFACE3D, + optional STAC-Import)
    stac_import  STAC-Import eines vorhandenen DIFF (nachholen)

Log: logging, WARNUNG:/FEHLER: als Praefix fuer das GUI-Log. Steuerzeilen fuer das GUI:
'PROGRESS:<0..1>' (Gesamtfortschritt) und 'PHASE:<text>' (laufender Schritt, z.B.
'Download …: 120/594 MB'). Bei info/s3d_search steht auf stdout nur das JSON, das Log geht
auf stderr.
Abbruch: das GUI legt die Marker-Datei 'cancel_file' an; geprueft wird zwischen den
Bloecken, im GDAL-Callback, beim Download und zwischen den Upload-Teilen.
Exit-Codes: 0 ok, 1 Fehler, 2 Abbruch, 3 DIFF fertig, aber STAC-Import fehlgeschlagen.
"""

import json
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXIT_OK, EXIT_ERROR, EXIT_CANCEL, EXIT_IMPORT_FAILED = 0, 1, 2, 3

logger = logging.getLogger("runner")


class _GuiFormatter(logging.Formatter):
    """WARNUNG:/FEHLER: vor die erste Zeile, Folgezeilen eingerueckt."""
    PREFIX = {logging.WARNING: "WARNUNG: ", logging.ERROR: "FEHLER: ", logging.CRITICAL: "FEHLER: "}

    def format(self, record):
        msg = record.getMessage()
        prefix = self.PREFIX.get(record.levelno, "")
        if prefix:
            msg = prefix + msg.replace("\n", "\n" + " " * len(prefix))
        return msg


class _WarningCollector(logging.Handler):
    """Sammelt die Warnungen fuer die Zusammenfassung am Ende."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        if record.levelno == logging.WARNING:
            self.messages.append(record.getMessage().splitlines()[0])


def _setup_logging(stream):
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(stream)
    h.setFormatter(_GuiFormatter())
    root.addHandler(h)
    collector = _WarningCollector()
    root.addHandler(collector)
    root.setLevel(logging.INFO)
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return collector


def _progress_printer(lo=0.0, hi=100.0):
    """progress(prozent 0..100, text) -> gedrosselte Steuerzeilen fuer das GUI:
    'PROGRESS:<0..1>' (Gesamtfortschritt) und 'PHASE:<text>' (was gerade laeuft, z.B.
    'Download …: 120/594 MB'). lo/hi: Ausschnitt des Gesamtbalkens fuer diesen Schritt."""
    last = {"t": 0.0, "p": -1.0, "text": None, "tt": 0.0}

    def progress(pct, text=None):
        frac = max(0.0, min(1.0, (lo + (hi - lo) * float(pct) / 100.0) / 100.0))
        now = time.time()
        if frac >= 1.0 or now - last["t"] >= 0.5 or frac - last["p"] >= 0.005:
            print(f"PROGRESS:{frac:.6f}", flush=True)
            last["t"], last["p"] = now, frac
        if text and text != last["text"]:
            # neue Phase sofort, laufende Zaehler (MB, Teile) hoechstens alle 0.5 s
            new_phase = (last["text"] or "").split(":")[0] != text.split(":")[0]
            if new_phase or now - last["tt"] >= 0.5:
                print(f"PHASE:{text}", flush=True)
                last["text"], last["tt"] = text, now
    return progress


def _fraction_progress(printer):
    """Adapter fuer Funktionen, die progress(anteil 0..1, text) melden (Multipart-Upload)."""
    return lambda frac, text=None: printer(100.0 * frac, text)


def _cancel_from(cfg):
    path = cfg.get("cancel_file")
    if not path:
        return None
    return lambda: os.path.exists(path)


def _proxy_path(secrets_dir):
    from _stac_http import PROXY_FILE
    return Path(secrets_dir) / PROXY_FILE if secrets_dir else None


# ─── Aktionen ─────────────────────────────────────────────────────────────────
def _info(cfg):
    """Datei: Raster-Info. STAC-Link: Asset-Metadaten (zum Vorbelegen der GUI-Felder) -
    die Datei selbst wird dafuer nicht geladen."""
    import dsm_diff
    import stac_import
    path = cfg["input_path"]
    if not stac_import.is_url(path):
        print(json.dumps(dsm_diff.describe_raster(path), ensure_ascii=False), flush=True)
        return
    from _stac_http import get_session
    info = {"path": path, "problem": None}
    try:
        st = stac_import.fetch_asset(get_session(_proxy_path(cfg.get("secrets_dir"))), path)
        info.update(stac=st, res_x=st["gsd"], vertical_stac=st["height_ref"],
                    srs_name=f"EPSG:{st['epsg']}" if st.get("epsg") else None,
                    size_mb=st.get("size_mb"))
    except Exception as e:
        info["problem"] = str(e)
    print(json.dumps(info, ensure_ascii=False), flush=True)


def _s3d_search(cfg):
    import dsm_diff
    import swisssurface3d as s3d
    from _stac_http import get_session
    import stac_import
    session = get_session(_proxy_path(cfg.get("secrets_dir")))
    neu = None
    if stac_import.is_url(cfg["dsm_neu"]):
        # Die Suche braucht nur die Ausdehnung: zuerst aus dem Dateikopf (Sekunden statt der
        # ganzen Datei); nur wenn das nicht reicht, wird die Datei in den Cache geladen
        with tempfile.TemporaryDirectory(prefix="dsmdiff_kopf_") as td:
            neu, _ = dsm_diff.remote_header_facts(cfg["dsm_neu"], "DSM aktuell", session, td)
        if neu is None:
            logger.info("DSM aktuell: Dateikopf reicht nicht - die ganze Datei wird geladen "
                        "(bleibt im Cache fuer den Lauf).")
            staging = Path(cfg.get("staging_dir") or tempfile.gettempdir())
            local, _ = dsm_diff.resolve_input(cfg["dsm_neu"], "DSM aktuell", staging, session)
            neu = dsm_diff.raster_facts(local, "DSM aktuell")
    else:
        neu = dsm_diff.raster_facts(cfg["dsm_neu"], "DSM aktuell")
    result = s3d.search(session, neu["bounds"], int(cfg["ref_year"]))
    # Fuer die GUI-Anzeige genuegen Jahre und Listen (hrefs bleiben weg)
    result["chosen"] = {k: v["year"] for k, v in result["chosen"].items()}
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _terrain_models(meta, line_ids, camera, alt_quelle, s3d_years, neu_link=None, alt_link=None):
    """DigitalSurfaceModel 1/2 der Asset-Beschreibung. Jahr Modell 1 = Jahr der ersten LineID,
    ein STAC-Link wird mit seinem Asset-Namen genannt (stac_import.terrain_model_label)."""
    import stac_import
    year = stac_import.acquisition_times(line_ids, camera)[0][:4]
    tm1 = stac_import.terrain_model_label(meta.get("tm1") or stac_import.TERRAIN_MODEL_1_DEFAULT,
                                          [year], neu_link)
    if alt_quelle == "swisssurface3d":
        tm2 = stac_import.terrain_model_label(stac_import.TERRAIN_MODEL_S3D, s3d_years)
    else:
        tm2 = stac_import.terrain_model_label(meta.get("tm2"), [meta.get("tm2_year") or ""],
                                              alt_link)
    return [tm1, tm2]


def _accuracy(meta, s3d, md=None):
    """Hoehengenauigkeit 1 sigma je DSM (GUI-Felder, sonst DIFF-Metadaten, sonst Default je
    Quelle: DSM-Spezifikation bzw. swissSURFACE3D) -> (sigma_neu, sigma_alt, VerticalAccuracy
    der Beschreibung). ValueError bei falscher Eingabe."""
    import stac_import
    md = md or {}
    s_neu = stac_import.sigma_text(meta.get("sigma_neu") or md.get("DSMDIFF_SIGMA_NEW")
                                   or stac_import.DEFAULT_SIGMA)
    s_alt = stac_import.sigma_text(meta.get("sigma_alt") or md.get("DSMDIFF_SIGMA_OLD")
                                   or (stac_import.DEFAULT_SIGMA_S3D if s3d
                                       else stac_import.DEFAULT_SIGMA))
    accuracy = stac_import.vertical_accuracy(s_neu, s_alt)
    logger.info(f"Hoehengenauigkeit 1 sigma: DSM aktuell {s_neu} m, DSM alt {s_alt} m "
                f"-> dh {accuracy}")
    return s_neu, s_alt, accuracy


def _dsm_diff(cfg):
    import dsm_diff
    import stac_import

    meta = cfg.get("meta") or {}
    stac = cfg.get("stac") or {}
    import_on = bool(stac.get("enabled"))
    env = str(stac.get("env") or stac_import.DEFAULT_ENV).upper()
    secrets_dir = stac.get("secrets_dir")
    alt_quelle = cfg.get("alt_quelle", dsm_diff.ALT_S3D)
    cancel = _cancel_from(cfg)
    # Mit Import: Rechnung 0-90 % des Balkens, Upload 90-100 %
    progress = _progress_printer(0, 90 if import_on else 100)
    upload_progress = _fraction_progress(_progress_printer(90, 100))
    camera = meta.get("camera") or stac_import.DEFAULT_CAMERA
    line_ids = meta.get("line_ids") or []
    area = (meta.get("area") or "").strip()

    logger.info("=== DSM-DIFF ===")
    logger.info(f"DSM aktuell : {cfg['dsm_neu']}")
    logger.info(f"DSM alt     : " + ("swissSURFACE3D Raster (STAC)" if alt_quelle == dsm_diff.ALT_S3D
                                     else cfg.get("dsm_alt", "")))
    logger.info(f"Ausgabe     : {cfg['out_path']}")
    logger.info("Vorzeichen  : dh = DSM aktuell - DSM alt (negativ = Abtrag/Verlust)")
    sigma_neu, sigma_alt, accuracy = _accuracy(meta, alt_quelle == dsm_diff.ALT_S3D)

    item = None
    extra = {"DSMDIFF_AREA": area, "DSMDIFF_SIGMA_NEW": sigma_neu, "DSMDIFF_SIGMA_OLD": sigma_alt,
             "DSMDIFF_VERTICAL_ACCURACY": accuracy}
    if line_ids:
        ids = stac_import.normalized_line_ids(line_ids, camera)
        extra["DSMDIFF_LINE_IDS"] = ",".join(ids)
        extra["DSMDIFF_CAMERA"] = camera
        try:
            item = stac_import.item_id(meta.get("auftragstyp") or stac_import.DEFAULT_AUFTRAGSTYP,
                                       ids, camera)
            extra["DSMDIFF_STAC_ITEM"] = item
        except ValueError:
            if import_on:
                raise
    if import_on and not item:
        raise ValueError("STAC-Import verlangt LineIDs (daraus kommt die Item-ID).")

    session = None
    auth = None
    if import_on:
        import _stac_http
        logger.info(f"STAC-Import vorgesehen: {env}, Item {item}, Asset "
                    f"{stac_import.asset_name(item)}")
        user, password, file_host = _stac_http.load_stac_credentials(secrets_dir, env)
        auth = (user, password)
        if file_host and file_host.strip().lower() != stac_import.STAC_HOSTS[env]:
            logger.warning(f"stac_credentials.json nennt fuer {env} den Host {file_host!r} - "
                           f"verwendet wird {stac_import.STAC_HOSTS[env]}.")
    needs_net = (import_on or alt_quelle == dsm_diff.ALT_S3D or stac_import.is_url(cfg["dsm_neu"])
                 or (alt_quelle == dsm_diff.ALT_FILE and stac_import.is_url(cfg.get("dsm_alt"))))
    if needs_net:
        from _stac_http import get_session
        session = get_session(_proxy_path(secrets_dir))

    state = {}
    if import_on:
        # Fruehe Item-Pruefung VOR jedem Download (STAC-Link, swissSURFACE3D) und vor der
        # Rechnung - der GSD-Vergleich folgt, sobald das Raster gelesen ist (pre_check)
        state["check"] = stac_import.check_item(session, env, item, line_ids, camera, area,
                                                None, bool(stac.get("replace_existing")))

    neu_link = cfg["dsm_neu"] if stac_import.is_url(cfg["dsm_neu"]) else None
    alt_link = (cfg.get("dsm_alt") if alt_quelle == dsm_diff.ALT_FILE
                and stac_import.is_url(cfg.get("dsm_alt")) else None)

    def pre_check(info):
        years = info["s3d"]["years"] if info.get("s3d") else []
        if import_on:
            state["description"] = stac_import.build_description(
                area, _terrain_models(meta, line_ids, camera, alt_quelle, years, neu_link, alt_link),
                camera, line_ids, meta.get("commentary") or stac_import.DEFAULT_COMMENTARY,
                accuracy=accuracy)
            logger.info(f"Asset-Beschreibung: {state['description']}")
            stac_import.check_gsd(state["check"], info["neu"]["res_x"])

    result = dsm_diff.run_dsm_diff(
        cfg["dsm_neu"], cfg["out_path"], dsm_alt=cfg.get("dsm_alt"), alt_quelle=alt_quelle,
        # Gleichnamige Ausgaben werden ersetzt (Standard, Wunsch Nutzer) - erst nach der
        # COG-Pruefung per os.replace, bei Fehler bleibt die alte Datei
        staging_dir=cfg.get("staging_dir"), overwrite=bool(cfg.get("overwrite", True)),
        block_size=int(cfg.get("block_size", 2048)), progress=progress, cancel=cancel,
        ref_year=cfg.get("ref_year"), mosaic_out=cfg.get("mosaic_out"), session=session,
        keep_staging=bool(cfg.get("keep_staging")), metadata=extra, pre_check=pre_check,
        out_gsd=cfg.get("out_gsd"))

    imported = None
    if import_on:
        try:
            # eo:gsd = Zelle des Diff; mit dem DSM-RASTER verglichen wird DSM aktuell
            imported = stac_import.run_stac_import(
                result["out"], env, item, state["description"], result["gsd"], line_ids,
                camera, area, bounds_lv95=result["bounds"], secrets_dir=secrets_dir,
                replace_existing=bool(stac.get("replace_existing")), cancel=cancel,
                progress=upload_progress, session=session, auth=auth,
                source_gsd=result["gsd_neu"])
        except Exception as e:      # inkl. InterruptedError (Abbruch waehrend des Uploads)
            what = "abgebrochen" if isinstance(e, InterruptedError) else "fehlgeschlagen"
            logger.error(f"STAC-Import {what}: {e}\n"
                         f"Das DIFF ist fertig und geprueft: {result['out']}\n"
                         f"Import spaeter ueber 'Nur STAC-Import' nachholen.")
            _summary(result, None)
            return EXIT_IMPORT_FAILED
    _summary(result, imported)
    return EXIT_OK


def _stac_import(cfg):
    """Import eines vorhandenen DIFF. DigitalSurfaceModel 2 (Jahre) kommt aus dessen Metadaten."""
    import struct
    from osgeo import gdal
    import dsm_diff
    import stac_import

    meta = cfg.get("meta") or {}
    stac = cfg.get("stac") or {}
    env = str(stac.get("env") or stac_import.DEFAULT_ENV).upper()
    camera = meta.get("camera") or stac_import.DEFAULT_CAMERA
    line_ids = meta.get("line_ids") or []
    area = (meta.get("area") or "").strip()
    path = cfg["diff_path"]
    item = stac_import.item_id(meta.get("auftragstyp") or stac_import.DEFAULT_AUFTRAGSTYP,
                               line_ids, camera)

    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    md = ds.GetMetadata() or {}
    st = ds.GetMetadata("IMAGE_STRUCTURE") or {}
    b = ds.GetRasterBand(1)
    gt = ds.GetGeoTransform()
    nd, srs, gsd = b.GetNoDataValue(), ds.GetSpatialRef(), gt[1]
    bounds = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
    dtype = b.DataType
    b = ds = None
    problems = []
    if not md.get("DSMDIFF_DEFINITION"):
        problems.append("keine DSMDIFF-Metadaten (nicht mit diesem Werkzeug erstellt?)")
    if st.get("LAYOUT") != "COG":
        problems.append("kein COG")
    if dtype != gdal.GDT_Float32 or nd is None or \
            struct.pack("<f", nd) != struct.pack("<f", dsm_diff.DIFF_NODATA):
        problems.append(f"Float32 mit NoData {dsm_diff.DIFF_NODATA!r} erwartet")
    if srs is None or not dsm_diff.horizontal_is_lv95(srs) or srs.IsCompound():
        problems.append("CRS EPSG:2056 (ohne Hoehenbezug) erwartet")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    if md.get("DSMDIFF_STAC_ITEM") and md["DSMDIFF_STAC_ITEM"] != item:
        raise ValueError(f"Das DIFF wurde fuer Item {md['DSMDIFF_STAC_ITEM']} erstellt, die "
                         f"LineIDs ergeben {item} - falsche Datei oder falsche LineIDs?")

    if md.get("DSMDIFF_SOURCE_OLD_TYPE") == "swissSURFACE3D":
        alt_quelle = "swisssurface3d"
        years = [y for y in md.get("DSMDIFF_SOURCE_OLD_YEARS", "").split(",") if y]
    else:
        alt_quelle, years = "datei", []
    logger.info(f"=== STAC-Import (vorhandenes DIFF) ===\nDIFF: {path}  (GSD {gsd:g} m)")
    # Hoehengenauigkeit: GUI-Felder (aus dem DIFF vorbelegt) gewinnen, Abweichung zum DIFF melden
    accuracy = _accuracy(meta, alt_quelle == "swisssurface3d", md)[2]
    if md.get("DSMDIFF_VERTICAL_ACCURACY") not in (None, accuracy):
        logger.warning(f"VerticalAccuracy {accuracy} weicht von den DIFF-Metadaten ab "
                       f"({md['DSMDIFF_VERTICAL_ACCURACY']}) - importiert wird die Eingabe.")
    # Links der Eingaben aus den DIFF-Metadaten (DSMDIFF_SOURCE_OLD ist bei einem Link der Link)
    old = md.get("DSMDIFF_SOURCE_OLD")
    description = stac_import.build_description(
        area, _terrain_models(meta, line_ids, camera, alt_quelle, years,
                              md.get("DSMDIFF_SOURCE_NEW_HREF") or None,
                              old if alt_quelle == "datei" and stac_import.is_url(old) else None),
        camera, line_ids, meta.get("commentary") or stac_import.DEFAULT_COMMENTARY,
        accuracy=accuracy)
    # Aeltere DIFF ohne DSMDIFF_GSD_NEW liegen im Gitter von DSM aktuell
    res = stac_import.run_stac_import(
        path, env, item, description, gsd, line_ids, camera, area, bounds_lv95=bounds,
        secrets_dir=stac.get("secrets_dir"), replace_existing=bool(stac.get("replace_existing")),
        cancel=_cancel_from(cfg), progress=_fraction_progress(_progress_printer()),
        source_gsd=float(md.get("DSMDIFF_GSD_NEW") or gsd))
    logger.info(f"\nFertig: {res['asset']} in {env}"
                + ("  (Item neu angelegt)" if res.get("item_created") else "")
                + f"\n  {res['browser_link']}")
    return EXIT_OK


def _summary(result, imported):
    s = result["stats"]
    lines = ["", "=== Zusammenfassung ===",
             f"DIFF       : {result['out']}",
             f"Quelle alt : {result['alt_desc']}",
             f"Resampling : {result['resampling']}",
             f"GSD        : {result['gsd']:g} m"
             + (f" (gemittelt aus {result['gsd_neu']:g} m)" if result.get("aggregated") else ""),
             f"dh gueltig : {s['valid_percent']:.2f} % der Pixel, ohne Referenz "
             f"{s['no_reference_percent']:.2f} % der Flaeche von DSM aktuell"]
    if result.get("mosaic"):
        lines.insert(3, f"Mosaik     : {result['mosaic']}")
    if s["valid"]:
        lines.append(f"dh [m]     : min {s['min']:.3f}, max {s['max']:.3f}, "
                     f"mittel {s['mean']:.3f}, std {s['std']:.3f}")
    if imported:
        lines.append(f"STAC       : {imported['asset']} in {imported['env']}"
                     + ("  (Item neu angelegt)" if imported.get("item_created") else ""))
        lines.append(f"             {imported['browser_link']}")
    for line in lines:
        logger.info(line)


ACTIONS = {"info": _info, "s3d_search": _s3d_search, "dsm_diff": _dsm_diff,
           "stac_import": _stac_import}


def main():
    if len(sys.argv) < 2:
        print("FEHLER: Kein Konfigurationspfad uebergeben.", flush=True)
        sys.exit(EXIT_ERROR)
    # utf-8-sig: eine von Hand (z.B. PowerShell 5.1) geschriebene Config traegt oft eine BOM
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        cfg = json.load(f)
    action = cfg.get("action", "")
    json_action = action in ("info", "s3d_search")
    collector = _setup_logging(sys.stderr if json_action else sys.stdout)

    if action not in ACTIONS:
        logger.error(f"Unbekannte Aktion: {action!r}")
        sys.exit(EXIT_ERROR)
    try:
        code = ACTIONS[action](cfg) or EXIT_OK
    except InterruptedError as e:
        logger.error(f"{e} - keine Ausgabe abgelegt, eine alte Ausgabe bleibt unveraendert.")
        sys.exit(EXIT_CANCEL)
    except Exception as e:
        logger.error(str(e))
        if not isinstance(e, ValueError):
            # ValueError = verstaendliche Pruefmeldung, alles andere mit Traceback
            logging.getLogger().info(traceback.format_exc())
        sys.exit(EXIT_ERROR)
    if not json_action and collector.messages:
        logger.info(f"\n{len(collector.messages)} Warnung(en):")
        for m in collector.messages:
            logger.info(f"  - {m}")
    sys.exit(code)


if __name__ == "__main__":
    main()
