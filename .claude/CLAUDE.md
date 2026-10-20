# CLAUDE.md – topo-rmDSMdiff (DSM-DIFF)

Werkzeug: dh = DSM aktuell − DSM alt als COG, optional als Asset ins STAC-Item der Befliegung.
Fachlicher Hintergrund, Ablauf und Konventionen: `README.md` im Projektordner.

## Ablage
- Hauptverzeichnis nur `GUI_DSMdiff.py` und `README.md` (plus Ordner, `.gitignore`, `.vscode`).
- `process_scripts/` Runner und Module, `test/` Tests, `secrets/` echte Zugangsdaten (git-ignoriert),
  `proxy_credentials/` die `*.examples`-Vorlagen, `.claude/CLAUDE.md` diese Regeln.

## Arbeitsweise
- Zuerst lesen, nicht raten. Kleinste Lösung, wenige Module, Funktionen statt Klassen.
- Kommentare Deutsch, `logging` statt `print` (Runner: Präfix `WARNUNG:`/`FEHLER:`), `pathlib`,
  keine hartcodierten absoluten Pfade (Ausnahme: Default-Staging `Y:\02_DMC_tempProcessingFolder`).
- Datasets explizit schliessen (`ds = None`), sonst bleiben Dateien unter Windows gesperrt.
- `git add/commit/push` macht der Nutzer selbst – auch nicht in Test-Repos ausführen.
- Am Ende berichten: geändert / getestet (mit Ausgabe) / nicht getestet / Risiken.

## Umgebung
- GUI (`GUI_DSMdiff.py`): Standard-Python, nur tkinter + Standardbibliothek, kein GDAL.
  Darstellung wie `topo-GDWHimport/GUI_GDWHimport.py` (Hell/Dunkel beachten, auch deaktivierte
  Elemente – clam zeichnet sie sonst hell).
- `process_scripts/`: OSGeo4W/QGIS-Python als Subprocess. Nur `osgeo`, `numpy`, `requests`,
  Standardbibliothek. Kein rasterio, ArcPy, pystac, multihash. GDAL ≥ 3.1.
- `stac_import.py` Teil 1 und `_line_ids.py` bleiben reine Standardbibliothek (das GUI importiert sie).
- Referenzprojekte (DMC-Converter, GDWH-Import, rapidmapping) nur lesen; Code kopieren, nicht
  importieren, Herkunft + Datum im Kopfkommentar. `_line_ids.py` ist eine 1:1-Kopie.

## Externe Schnittstelle STAC – nicht nebenbei ändern
- Collection `ch.swisstopo.spezialbefliegungen`, Item `<auftragstyp>-<StacItemIdDatetime>`
  (Logik `_line_ids.stac_datetime`), Asset `<item>-dsm-diff.tif` klein, Titel `DSM-DIFF`.
- Beschreibung als Text in einer Funktion (`stac_import.build_description`), Muster der GDWH-Assets,
  Schlüssel `TerrainModel 1` / `TerrainModel 2`. Test gegen das echte DSM-RASTER-Asset.
  Werte (Entscheid Nutzer, `stac_import.terrain_model_label`): `<JAHR>-<Name>`, z.B.
  `2026-DSM-photogrammetric`, `2025-swissSURFACE3D-DSM` (Jahre der Kacheln mit `/`); bei einem
  STAC-Link zusätzlich der Asset-Name in Klammern (nicht der ganze href), lokale Pfade nie.
- **INT ist immer Default.** PROD nie aus Versehen, im GUI nur nach Bestätigungsdialog.
- **Ein bestehendes Item nie schreiben** (kein PUT/PATCH auf `/items/<id>`), dort nur Asset-PUT +
  Multipart-Upload. `publish_to_stac` aus rapidmapping nicht verwenden.
- **Fehlendes Item anlegen** (Entscheid Nutzer: das DIFF kann vor DSM-RASTER/DOP in STAC kommen):
  `stac_import.create_item` – vorher nochmals GET (404), dann PUT mit id, Geometrie = Perimeter
  DSM aktuell (WGS84), `properties.datetime` = erste LineID, kein Titel. PUT-Antwort 200 statt 201
  = Item entstand inzwischen -> Abbruch. Bei gescheitertem Upload neues Asset und leeres, in diesem
  Lauf angelegtes Item wieder entfernen. Der spätere GDWH-STAC-Import ins selbe Item lässt das
  DIFF-Asset stehen (vom Nutzer bestätigt, gleich wie bei den Quick-DOPs).
- Vorhandenes Item: datetime falsch -> Abbruch vor der Rechnung. DSM-RASTER fehlt -> nur Info.
  Area/GSD-Abweichung zum DSM-RASTER-Asset: nur Warnung. Diff-Asset vorhanden ohne „ersetzen“: Abbruch.
  Die Item-Prüfung läuft im Runner VOR jedem Download, der GSD-Vergleich nach dem Lesen (`check_gsd`).
- Ausgabedateien: das GUI ersetzt gleichnamige Dateien immer (Entscheid Nutzer), aber erst nach der
  COG-Pruefung per `os.replace`. Kernfunktion/CLI behalten `overwrite` (Default False).

## Eingaben
- DSM aktuell / DSM alt: lokaler Pfad (GeoTIFF/COG + TFW) **oder STAC-Link** (href bzw. API-URL eines
  Assets auf INT/PROD). Ein Link wird ins Staging geladen (`<staging>/stac_asset_cache/`, Prüfsumme
  gegen `checksum:multihash`), nicht per `/vsicurl/` gestreamt (Proxy/Kerberos).
- Bei einem Link belegt das GUI aus den STAC-Metadaten vor: Area, CameraSystem, LineIDs, Jahr
  (DSM aktuell) bzw. TerrainModel 2 + Jahr (DSM alt). Den Auftragstyp ändern die DSM-Eingaben nie
  (Entscheid Nutzer: bleibt `ram`, auch bei einem `kry`-Item); nur „Nur STAC-Import“ übernimmt ihn
  aus den `DSMDIFF_*`-Metadaten des DIFF.
- swissSURFACE3D: Jahr je Kachel = jüngstes **vor** dem Aufnahmejahr, feste Regel (die Option
  „gleiches Jahr“ hat der Nutzer gestrichen; die Kernfunktion kennt `allow_same_year` nur noch für Tests).

## Credentials
- `secrets/` (stac_credentials.json, proxy_config.json) steht in `.gitignore`. Echte Secret-Dateien
  nie lesen oder ausgeben, nur die Struktur aus `proxy_credentials/*.examples`.
- Credentials nie loggen, nie in Fehlermeldungen; vorsignierte S3-URLs ebenfalls nicht loggen.

## Raster-Regeln
- NoData `-3.4028234663852886e+38` (−FLT_MAX) im Diff und im swissSURFACE3D-Mosaik (Kacheln
  liefern −9999 → wird umgesetzt), bitgenau zurücklesen.
- Referenzgitter = DSM aktuell. Resampling nur bei anderem Gitter: bilinear (gröber/versetzt),
  average (feiner), immer mit Warnung.
- `gdal.Warp` immer mit `srcSRS="EPSG:2056"` **und** `dstSRS="EPSG:2056"` (sonst Höhentransformation
  zwischen Compound-CRS). Nie still umprojizieren; horizontal nicht LV95 → Abbruch.
- CRS-Tag Diff nur `EPSG:2056`; Mosaik `EPSG:2056+5728`.
- Höhenbezug aus CRS-Tag, STAC-Beschreibung (`SourceReferenceSystem … LV95_LN02`, zählt wie ein Tag)
  oder Dateiname (nur Zusatzindiz). Widerspruch zwischen Quellen oder verschiedene Höhenbezüge →
  Abbruch. swissSURFACE3D muss LN02 sein: Assetname `_2056_5728` (alle Kacheln) + CRS-Tag einer Kachel.
- Prüfungen vor dem ersten Schreiben; Ausgabe erst nach bestandener COG-Prüfung per `os.replace`;
  Staging aufräumen (auch bei Fehler/Abbruch); alte Ausgabe bleibt bei Fehler unberührt.
- Statistik per `SetStatistics` im Staging-TIFF (nicht `ComputeStatistics` auf dem COG → aux.xml).
- Keine Volumenberechnung anbieten (Entscheid mit dem Chef).

## Tests
- `test/test_functions.py`, synthetisch, ohne Netz, ohne echte Uploads.
- QGIS-Python: `test\run_pytest_osgeo.py <site-packages mit pytest>`; System-Python: `python -m pytest test`.
