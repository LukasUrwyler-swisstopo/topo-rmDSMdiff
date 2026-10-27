# DSM-DIFF

Werkzeug mit GUI für **DSM aktuell − DSM alt → ein Differenzraster als COG**, danach optional der
**direkte Import als Asset `<item>-dsm-diff.tif` in das bestehende STAC-Item** der Befliegung
(Collection `ch.swisstopo.spezialbefliegungen`).

Als Referenz („DSM alt“) dient standardmässig **swissSURFACE3D Raster** aus dem swisstopo-STAC:
die Kacheln, die DSM aktuell überlappen, werden automatisch gesucht, geladen und zu einem Mosaik
(Merged TIFF) zusammengesetzt. Alternativ ein eigenes DSM per Pfad.

Es gibt bewusst **keine Volumenberechnung**: ein Volumen hängt am Perimeter und kann falsch
definiert werden. Wer ein Volumen braucht, rechnet es mit DSM, Diff und dem eigenen Polygon.

## GUI starten

```bash
cmd> python GUI_DSMdiff.py
```
<img width="955" height="964" alt="image" src="https://github.com/user-attachments/assets/9e19aa6c-552a-4cfa-86ba-f417a5f5f08f" />

Das GUI läuft im Standard-Python (nur `tkinter`). Beim ersten Start sucht es die
OSGeo4W/QGIS-Installation selbst; „Ändern…“ setzt den Pfad von Hand. OSGeo4W-Python,
Staging- und Secrets-Ordner werden in `process_scripts/_dsmdiff_config.json` gespeichert
(lokal, nicht im Repository). Darstellung wie `topo-GDWHimport` (Dark Mode als Standard,
Umschalter „Hell“/„Dark“ oben rechts).

## GUI-Felder

| Abschnitt | Feld | Bemerkung |
|---|---|---|
| oben | OSGeo4W Python, Staging-Ordner, „Staging behalten“ | Staging Standard `Y:\02_DMC_tempProcessingFolder` (wie DMC-Converter) |
| **DSM aktuell** | Lokales DSM (Pfad) **oder STAC-Link** | Lokal (GeoTIFF/COG + TFW): Info-Zeile GSD, Grösse, CRS, Höhenbezug, NoData; Name `<JAHR>_<AREA>_DSM_<GSD>cm_LV95_<LN02\|LHN95>.tif` belegt Jahr, Area und Ausgabenamen vor. STAC-Link (href eines Assets, z.B. `…/<item>-dsm-raster.tif`): Area, CameraSystem, LineIDs und Jahr aus den STAC-Metadaten; der Auftragstyp bleibt (Standard `ram`, auch bei einem `kry`-Item) |
| **DSM alt** | ● swissSURFACE3D (Standard) | Kacheln über die Ausdehnung von DSM aktuell; Jahr je Kachel aus dem STAC-Item, gewählt das jüngste **vor** dem Aufnahmejahr (feste Regel); Button „Kacheln suchen“ (amber) zeigt Kacheln, Jahre, fehlende Kacheln; danach „Kacheln geladen“ (grün), bis sich DSM aktuell oder das Aufnahmejahr ändert; grau bei Eigenem DSM |
| | ○ Eigenes DSM (Pfad oder STAC-Link) | ein Band, gleiche Prüfungen wie DSM aktuell; Höhenbezug aus Tag, STAC-Beschreibung oder Dateiname (`_LV95_LN02`); bei einem Link werden DigitalSurfaceModel 2 und Jahr vorbelegt |
| **Meta-Informationen / STAC** | Auftragstyp (Standard `ram`), CameraSystem (Standard `Leica DMC-4`), Area, Jahr, DigitalSurfaceModel 1, DigitalSurfaceModel 2, Höhengenauigkeit je DSM (1σ; Standard DSM aktuell `0.5-1` m, DSM alt `0.3` m bei swissSURFACE3D bzw. `0.5-1` m bei eigenem DSM), SourceRefSys (fix), Commentary, LineIDs | Listen wie `topo-GDWHimport`, bei den Modellen ohne DTM (nur photogrammetrisches DSM und swissSURFACE3D). LineIDs per Excel-Paste, geprüft, sortiert, ohne Duplikate (`_line_ids.normalize`). Höhengenauigkeit als Zahl oder Bereich, siehe STAC-Import |
| | Vorschau STAC | StacItemIdDatetime, Item-ID, Asset-Name, Titel, Browser-Link (klickbar), kompletter Beschreibungstext |
| **Ausgabe** | Auflösung DIFF (GSD) | Dropdown `1 m` (Standard) / `0.5 m`, siehe Konventionen |
| | DIFF-COG (.tif) | Vorschlag `<JAHR>_<AREA>_DSM-DIFF_<GSD>cm_LV95.tif` (GSD des DIFF, folgt dem Dropdown, solange der Name nicht von Hand geändert wurde); Mosaik `<JAHR>_<AREA>_swissSURFACE3D_50cm_LV95_LN02.tif` daneben. Gleichnamige Dateien werden **immer ersetzt** – erst nach bestandener Prüfung, bei Fehler bleibt die alte Datei |
| **STAC-Import** | Checkbox, Umgebung **INT (Standard)** / PROD, Secrets-Ordner, „vorhandenes STAC-Asset ersetzen“ | PROD nur nach Bestätigungsdialog mit Item-ID und Asset-Name. Fehlt das Item, wird es angelegt |
| | Nur DIFF-COG STAC-Import (ohne DIFF-Generierung) + „▶ Nur STAC-Import dieses DIFF“ | Import eines schon gerechneten DIFF ohne neue Rechnung. Beim Wählen der Datei werden Area, LineIDs, Auftragstyp, CameraSystem und Höhengenauigkeit aus den `DSMDIFF_*`-Metadaten vorbelegt; nach jedem Lauf steht das neue DIFF hier schon drin |
| unten | ▶ DSM-DIFF BERECHNEN, Abbrechen, Log löschen, Terminal | Start-Button amber = Angaben fehlen, grün = startbereit |

Das Aufnahmejahr (von DSM aktuell) kommt aus dem Datum der ersten LineID, ohne LineIDs aus dem
Feld Jahr. Pro swissSURFACE3D-Kachel gibt es oft mehrere Jahre (z.B. 2017 und 2024); das gleiche
Jahr wie die Aufnahme wird nie genommen, weil die STAC-Items nur `YYYY-01-01` tragen und offen
bliebe, ob die Lidar-Befliegung vor oder nach der Aufnahme war.

**STAC-Link als Eingabe:** erlaubt sind die href eines Assets
(`https://data.geo.admin.ch/<collection>/<item>/<asset>.tif`, INT `sys-data.int.bgdi.ch`) oder
dessen API-URL. Die Datei wird beim Lauf in den Cache `<Staging>\stac_asset_cache\<item>\` geladen
(Prüfsumme gegen `checksum:multihash`, gültige Datei wird nicht neu geladen) und dann wie eine
lokale Datei gerechnet – bewusst kein Streaming über `/vsicurl/`, weil GDAL den Firmen-Proxy
(Kerberos) nicht zuverlässig nutzt. Das DSM-RASTER-Asset trägt im CRS-Tag nur `EPSG:2056`; der
Höhenbezug kommt dann aus der Beschreibung (`SourceReferenceSystem: … LV95_LN02`) und zählt wie
ein Tag. „Kacheln suchen“ liest bei einem Link nur den **Dateikopf** (1 MB, notfalls 8 MB): ein
COG hat seine Kopfdaten am Dateianfang, GDAL liest daraus Grösse und Ausdehnung (am echten
Guppenfirn-DSM: 2.3 s statt 594 MB). Nur wenn der Kopf nicht reicht (kein COG), wird die ganze
Datei geladen; die volle Datei holt sonst erst der Lauf.

Rückmeldung im GUI: Während Info und „Kacheln suchen“ zeigt der Mauszeiger eine Sanduhr und die
Zeile darunter, was läuft. Beim Lauf steht neben dem Fortschrittsbalken die Phase (z.B.
„Download …-dsm-raster.tif: 120/594 MB“, „Differenzrechnung“, „COG schreiben“, „Upload Teil
3/7“); die Restzeit wird erst für die Rechnung geschätzt, bei Download/Upload zeigt die Phase den
Stand in MB bzw. Teilen.

## Ablauf

```
DSM aktuell ──┐                                    Prüfungen (vor dem ersten Schreiben):
              │                                    Pfade, 1 Band, LV95, nicht rotiert, north-up,
              │                                    Meter, Höhenbezug, Überlappung, STAC-Item
swissSURFACE3D-STAC                                   │
  Suche (bbox, Paging) → Jahr je Kachel → Assetname _2056_5728 prüfen (LN02)
  → Download in den Cache (Prüfsumme) → CRS-Tag einer Kachel prüfen
  → VRT → Mosaik-COG (NoData -9999 → -3.4028235e+38, EPSG:2056+5728)
              │
              ▼
gleiches Gitter? ── ja → direkt lesen
              └── nein → gdal.Warp ins Staging (bilinear / average, srcSRS = dstSRS = EPSG:2056)
              ▼
dh = neu − alt blockweise (2048 px), float64 → Float32, Maskenband beider Raster
              ▼
Ausgabe-GSD gröber (Standard 1 m)? ── ja → gdal.Warp average auf Vielfache der GSD in LV95
              ▼
Metadaten + Statistik → COG (Temp) → Prüfung → os.replace an den Zielort → Staging aufräumen
              ▼
optional STAC: Item prüfen (fehlt → anlegen) → Asset anlegen (PUT asset) → Multipart-Upload → Kontrolle
```

## Konventionen

| Thema | Regel |
|---|---|
| Vorzeichen | **dh = DSM aktuell − DSM alt**, negativ = Abtrag / Verlust |
| Gitter | Gerechnet wird im Gitter von DSM aktuell (gleiche Ausdehnung, gleiche Geotransformation). Bei Auflösung DIFF = Zelle von DSM aktuell (z.B. 0.5 m) liegt das Diff pixelgenau auf dem DSM-RASTER-Asset |
| Auflösung DIFF | GUI-Auswahl **1 m (Standard, Wunsch Vorgesetzter)** oder 0.5 m. Gröber als DSM aktuell: erst dh im Gitter von DSM aktuell, dann `gdal.Warp` **average** (ab GDAL 3.1 flächengewichtet, NoData zählt nicht) auf ein Gitter mit Vielfachen der GSD in LV95 (bei 1 m ganze Meter), Ausdehnung nach aussen gerundet. Zuerst die Differenz, dann mitteln: jede Zelle stammt nur aus Pixeln, die in beiden DSM gültig sind. Feiner als DSM aktuell → Abbruch. Statistik, `eo:gsd` und Dateiname (`…_DSM-DIFF_100cm_LV95.tif`) gelten für die Ausgabe-GSD |
| Resampling | nur wenn DSM alt nicht im selben Gitter liegt: **bilinear**, wenn gröber oder nur versetzt, **average**, wenn feiner; immer mit Warnung, steht in den Metadaten |
| NoData | `-3.4028234663852886e+38` (−FLT_MAX), wie DSM-RASTER / GDWH SB_DSM – im Diff **und** im Mosaik |
| CRS Diff | nur `EPSG:2056` – dh ist eine Höhendifferenz, keine LN02-Höhe. Höhenbezug der Quellen in den Metadaten |
| CRS Mosaik | `EPSG:2056+5728` (LV95 + LN02) |
| Höhenbezug | verschieden → **Abbruch** (LN02−LHN95: −0.06 bis −0.30 m). Quellen: CRS-Tag, STAC-Beschreibung (bei einem Link, zählt wie ein Tag), Dateiname als Zusatzindiz. Widerspruch zwischen Quellen → Abbruch, nur Name → Warnung, nichts → Warnung „nicht geprüft“ |
| swissSURFACE3D | muss LN02 sein. Das STAC nennt den Höhenbezug nur im Assetnamen `_<gsd>_<EPSG horiz.>_<EPSG vert.>` (Collection und Assets: nur `proj:epsg 2056`, Stand 20.10.2026). Geprüft bei allen Kacheln (Name) und bei einer Kachel (CRS-Tag, falls vorhanden). LHN95 → Abbruch vor dem Download |
| Umprojektion | nie still – horizontal nicht LV95 → Abbruch |
| COG | `COMPRESS=DEFLATE`, `PREDICTOR=FLOATING_POINT`, `BLOCKSIZE=512`, `OVERVIEWS=AUTO`, `RESAMPLING=AVERAGE`, `BIGTIFF=IF_SAFER`; geprüft: Layout, Kompression, Predictor, NoData bitgenau, Gitter, CRS, Einheit `metre`, Overviews, keine `.aux.xml` |
| Fehlende Kacheln | swissSURFACE3D-Kachel fehlt oder hat kein passendes Jahr → Diff dort NoData, Warnung mit Liste; gemischte Jahre → deutliche Warnung |

TIFF-Metadaten des Diff (englisch, weil die Datei über STAC öffentlich wird):
`DSMDIFF_DEFINITION`, `DSMDIFF_SOURCE_NEW`, `DSMDIFF_SOURCE_OLD`, `DSMDIFF_SOURCE_OLD_TYPE`,
`DSMDIFF_SOURCE_OLD_YEARS`, `DSMDIFF_SOURCE_OLD_TILES`, `DSMDIFF_HEIGHT_REF_NEW/OLD`,
`DSMDIFF_RESAMPLING`, `DSMDIFF_GSD` (Zelle des Diff), `DSMDIFF_GSD_NEW` (Zelle von DSM aktuell),
`DSMDIFF_AGGREGATION` (Mitteln ja/nein), `DSMDIFF_LINE_IDS`, `DSMDIFF_STAC_ITEM`, `DSMDIFF_AREA`,
`DSMDIFF_SIGMA_NEW/OLD` (Höhengenauigkeit 1σ je DSM, z.B. `0.5-1`), `DSMDIFF_VERTICAL_ACCURACY`
(wie in der STAC-Beschreibung), `DSMDIFF_CREATED`,
`DSMDIFF_SOFTWARE`, `TIFFTAG_DATETIME`, Banddescription, Einheit `metre`, Statistik
(min/max/mittel/std/Anteil gültig). Alles im TIFF selbst, keine aux.xml.

Die Caches `<Staging>\swisssurface3d_cache\` und `<Staging>\stac_asset_cache\` bleiben über Läufe
bestehen – gültige Dateien (Prüfsumme) werden nicht neu geladen. Aufgeräumt wird nur der
Laufordner; die Caches bei Platzbedarf von Hand leeren.

## STAC-Import

- **Item-ID** = `<auftragstyp>-<StacItemIdDatetime>`, z.B. `kry-2026-08-13t08222100`
  (Linienstart der chronologisch ersten LineID, Logik aus `_line_ids.py` von `topo-GDWHimport`).
- **Asset** = `<item>-dsm-diff.tif` (klein geschrieben), Titel **`DSM-DIFF`**, ein Diff pro Item.
- **Ein bestehendes Item wird nie geschrieben** – kein PUT/PATCH auf das Item, es gehört dem
  GDWH-Import. `publish_to_stac` aus rapidmapping ist deshalb bewusst nicht übernommen (es
  überschreibt das Item mit neuer Geometrie und neuen Links).
- **Fehlt das Item, wird es angelegt.** Das DIFF kann das erste Asset sein: es wird aus dem frisch
  gerechneten DSM erstellt, bevor DSM-RASTER und DOP über die GDWH-&-STAC-Pipeline kommen. Neues
  Item = `id`, Geometrie = Perimeter von DSM aktuell (WGS84), `properties.datetime` = Linienstart
  der ersten LineID (wie beim GDWH-Import, z.B. `2026-08-13T08:22:21Z`), kein Titel (die
  GDWH-Items haben auch keinen). Die API kennt kein reines „Anlegen“ (`PUT` legt an **oder**
  aktualisiert, Antwort 201 bzw. 200) – deshalb wird unmittelbar vor dem PUT nochmals geprüft, ob
  das Item fehlt, und eine Antwort 200 statt 201 bricht mit deutlicher Meldung ab.
- Vor jedem Download und vor der Rechnung geprüft: LineIDs, Credentials lesbar, Item (falls vorhanden):
  `properties.datetime` = erste LineID, sonst Abbruch (falsches Item). Hat das Item schon ein
  `<item>-dsm-raster.tif`, werden dessen `eo:gsd` und `Area:` mit DSM aktuell und Area verglichen –
  **Abweichung = Warnung** (Entscheid). Verglichen wird die Zelle von DSM aktuell, nicht die des
  Diff: ein auf 1 m gemitteltes Diff trägt `eo:gsd 1`, das DSM-RASTER weiter `0.5`. Diff-Asset schon da → Abbruch, ausser „vorhandenes
  STAC-Asset ersetzen“.
- Scheitert der Upload, wird ein neu angelegtes Asset wieder entfernt, ebenso ein in diesem Lauf
  angelegtes Item, sofern es danach keine Assets hat.
- Beschreibung (Text, Reihenfolge wie die GDWH-Assets). Die beiden DSM stehen unter
  `DigitalSurfaceModel 1` (aktuell) und `DigitalSurfaceModel 2` (alt) – bis 2026-10-27 hiessen die
  Schlüssel `TerrainModel 1/2`, solche Beschreibungen werden weiterhin gelesen. Werte als `<JAHR>-<Name>`:
  lokales DSM `2026-DSM-photogrammetric`, swissSURFACE3D `2025-swissSURFACE3D-DSM` (Jahr der
  Kacheln, mehrere mit `/`, z.B. `2021/2023-…`). Ein DTM (swissALTI3D, DHM25) steht nicht zur
  Auswahl – das DSM-DIFF wird nie mit einem DTM gerechnet. Enthält der Name von DSM aktuell oder
  DSM alt (Dateiname bzw. Asset-Name des STAC-Links, nicht der Ordner) `DTM` als eigenes Wort oder
  `swissALTI` (Gross-/Kleinschreibung egal), verweigert das GUI den Start und die Kernfunktion
  bricht vor dem ersten Download ab. Mehr als der Name lässt sich nicht prüfen. Bei einem
  STAC-Link steht der Asset-Name in Klammern, z.B.
  `2026-DSM-photogrammetric (kry-2026-08-13t08222100-dsm-raster.tif)`; ein lokaler Pfad nie.
  Jahr DigitalSurfaceModel 1 = Jahr der ersten LineID, DigitalSurfaceModel 2 = Feld „Jahr“ (beim
  STAC-Link vorbelegt).
  `Unit: metre` (Einheit der dh-Werte) steht fest im Code, ohne Feld im GUI.
  `VerticalAccuracy` = Höhengenauigkeit des dh (1σ) und Nachweisgrenze LoD95, aus den Feldern
  „Höhengenauigkeit“ je DSM: σ_dh = √(σ_aktuell² + σ_alt²) (zwei Befliegungen, Fehler
  unabhängig), LoD95 = 1.96 · σ_dh, bei einem Bereich je für Unter- und Obergrenze. Änderungen
  kleiner als LoD95 sind statistisch nicht vom Rauschen zu unterscheiden. Bewusst nicht
  `StandardDeviation` – das wäre mit der Statistik `STATISTICS_STDDEV` der dh-Werte im COG zu
  verwechseln. Standardwerte (Entscheid, im GUI änderbar):

  | DSM | σ (1σ) | Quelle |
  |---|---|---|
  | photogrammetrisches DSM (aktuell oder eigenes DSM alt) | `0.5-1` m | Spezifikation der DSM-Raster |
  | swissSURFACE3D Raster | `0.3` m | swissALTI3D-Produktinfo (swisstopo, März 2022) Kap. 1.4: 50-cm-Raster aus neuen LiDAR-Daten ±30 cm 1σ (Lage und Höhe). Die ±10 cm Höhe (Kap. 3.2.2, auch [Produktseite swissSURFACE3D Raster](https://www.swisstopo.admin.ch/de/hoehenmodell-swisssurface3d-raster)) gelten für die LiDAR-Basisdaten – 0.3 m steht wie der DSM-Wert auf Ebene Raster |

  Beim Umschalten von DSM alt wechselt der Default mit, ein von Hand geänderter Wert bleibt.
  Andere Werte (z.B. für ein älteres photogrammetrisches DSM) von Hand eintragen.
  Bei 1 m bleibt `VerticalAccuracy` unverändert (konservativ): Mitteln über 2 × 2 Pixel würde
  σ nur bei unkorrelierten Fehlern halbieren. Die Fehler photogrammetrischer DSM sind aber räumlich
  stark korreliert (Matching-Fenster, Interpolation), systematische Anteile (Höhenversatz,
  Blockverkippung zwischen den Befliegungen) bleiben beim Mitteln ganz stehen.
  ```
  Area: GUPPENFIRN, DigitalSurfaceModel 1: 2026-DSM-photogrammetric, DigitalSurfaceModel 2: 2025-swissSURFACE3D-DSM, SourceReferenceSystem: (EPSG:2056) CH1903+ / LV95_LN02, Unit: metre, VerticalAccuracy: 0.58-1.04 (1 sigma; LoD95 1.14-2.05), CameraSystem: Leica DMC-4, Acquisition time: 2026-08-13T08:22:21.00,2026-08-13T08:27:50.00, LineId: 20260813_0822_41216_003_082221,20260813_0822_41216_004_082750, Commentary: Digital Surface Model - Difference (DigitalSurfaceModel 1 minus DigitalSurfaceModel 2, negative = loss)
  ```
- Upload: Multipart wie rapidmapping, Prüfsumme sha2-256-Multihash mit hashlib, danach Kontrolle
  (`checksum:multihash` = lokal, `href` erreichbar) und Browser-Link im Log.
- Umgebungen: INT `sys-data.int.bgdi.ch` (Standard), PROD `data.geo.admin.ch`, API `/api/stac/v0.9/`.
- Schlägt nur der Import fehl, bleibt das fertige Diff liegen (Exit-Code 3, eigener Hinweis im GUI).
  Das DIFF steht danach im Abschnitt STAC-Import unter „Nur DIFF-COG STAC-Import“; nachholen mit
  **„▶ Nur STAC-Import dieses DIFF“** – DigitalSurfaceModel 2 (Jahre) kommt dann aus den Metadaten des
  Diff, die Item-ID aus den LineIDs muss zu `DSMDIFF_STAC_ITEM` passen. Die Höhengenauigkeit wird
  aus dem DIFF vorbelegt; weicht die Eingabe davon ab, gilt die Eingabe (mit Warnung).
- Das Diff geht **direkt nach STAC, nicht über den GDWH** – es liegt also nicht im GDWH-Archiv.
  Kommen DSM-RASTER und DOP später über die GDWH-STAC-Pipeline in dasselbe Item, bleibt das
  DSM-DIFF-Asset bestehen – so ist es bereits bei den Quick-DOPs (direkt in ein neues Item, das
  genaue DOP/DSM mit AT folgt später über die Pipeline ins selbe Item).

### Credentials

`secrets\stac_credentials.json` (Vorlage `proxy_credentials\stac_credentials.examples`, Aufbau wie
`topo-rapidmapping-int`), alternativ Umgebungsvariablen `STAC_USERNAME` / `STAC_PASSWORD`.
Proxy: `secrets\proxy_config.json` (Vorlage `proxy_credentials\proxy_config.examples`), Reihenfolge direkt →
System-Proxy (inkl. Kerberos, falls `pyspnego` installiert) → konfigurierte Proxies.
**`secrets/` steht in `.gitignore`.** Credentials werden nie geloggt und erscheinen in keiner
Fehlermeldung.

### Falsch hochgeladenes Asset entfernen

Nur das Asset löschen, **nie ein Item mit weiteren Assets** (DSM-RASTER, DOP … gehören dem
GDWH-Import). Ein Item, das dieses Werkzeug angelegt hat und das nur das DIFF enthielt, kann danach
leer stehen bleiben – es wird beim nächsten GDWH-Import ohnehin befüllt.

```
call "C:\Program Files\QGIS 3.44.15\bin\o4w_env.bat"
cd process_scripts
python -c "import _stac_http as h, stac_import as s; u,p,_=h.load_stac_credentials(r'..\secrets','INT'); print(s.delete_asset(h.get_session(r'..\secrets\proxy_config.json'),'INT','<item>','<item>-dsm-diff.tif',(u,p)))"
```

`True` = gelöscht. Für PROD `'INT'` zweimal durch `'PROD'` ersetzen. `topo-rapidmapping-int` hat
dafür `utilities/util_stac_delete_ram.py` – Achtung: das löscht ganze RAM-**Items**, für ein
einzelnes Diff-Asset nicht verwenden.

## Ordner

```
GUI_DSMdiff.py        GUI (starten)
README.md             diese Anleitung
process_scripts/      Runner und Module (OSGeo4W/QGIS-Python), _dsmdiff_config.json (lokal)
secrets/              stac_credentials.json, proxy_config.json  – nicht im Repository
proxy_credentials/     stac_credentials.examples, proxy_config.examples (Aufbau der Secrets)
test/                 pytest-Tests und Starter für das QGIS-Python
logs/                 Logdatei pro Lauf (entsteht beim ersten Lauf, nicht im Repository)
.claude/CLAUDE.md     Projektregeln für Claude Code
.vscode/              Analysepfad für Pylance (process_scripts)
```

## Architektur

```
GUI_DSMdiff.py                     Standard-Python, tkinter (Stil topo-GDWHimport)
        │  JSON-Config (tempfile), Abbruch über Marker-Datei
        ▼
process_scripts/_osgeo_runner.py   OSGeo4W/QGIS-Python: info | s3d_search | dsm_diff | stac_import
    dsm_diff.py                    Prüfungen, Mosaik, Resampling, Blockrechnung, COG, CLI
    swisssurface3d.py              STAC-Suche, Jahreswahl, Höhenbezug, Download, VRT
    stac_import.py                 Namen/Beschreibung (stdlib, auch GUI) + Item-Prüfung, Asset, Upload
    _stac_http.py                  Credentials, Proxy, Multipart (Auszüge topo-rapidmapping-int)
    _line_ids.py                   1:1-Kopie aus topo-GDWHimport
        │  stdout: Log (WARNUNG:/FEHLER:), PROGRESS:<0..1>, PHASE:<Schritt>
        ▼
logs/*.log
```

Exit-Codes des Runners: 0 ok, 1 Fehler, 2 Abbruch, 3 Diff fertig / Import fehlgeschlagen.

CLI ohne GUI (im OSGeo4W-Python, aus `process_scripts`):
```
python dsm_diff.py DSM_neu.tif DIFF.tif --alt DSM_alt.tif
python dsm_diff.py DSM_neu.tif DIFF.tif --ref-year 2026 --secrets-dir ..\secrets
python dsm_diff.py https://data.geo.admin.ch/<collection>/<item>/<item>-dsm-raster.tif DIFF.tif --ref-year 2026
```

## Voraussetzungen

- **GUI:** Python ≥ 3.6 mit `tkinter` (Syntax auf 3.6 geprüft; getestet mit 3.14).
- **Verarbeitung:** OSGeo4W- oder QGIS-Python mit `osgeo` (GDAL ≥ 3.1, COG-Treiber), `numpy`,
  `requests`, `urllib3`. Kein rasterio, kein pystac, kein multihash. Die GDAL-Version steht am
  Anfang jedes Logs. Entwickelt mit QGIS 3.44.15 / GDAL 3.13.3.
- **Netz:** Zugriff auf `data.geo.admin.ch` (swissSURFACE3D) und für den Import auf den
  STAC-Host der gewählten Umgebung.

## Tests

```
:: GDAL-Tests im QGIS-Python (pytest kommt aus einer anderen Installation, hinten an sys.path)
call "C:\Program Files\QGIS 3.44.15\bin\o4w_env.bat"
python test\run_pytest_osgeo.py C:\Python314\Lib\site-packages -q

:: GUI- und reine Python-Tests im System-Python
python -m pytest -q test
```

Synthetisch, ohne Netz, ohne echte Uploads: Diff exakt im gleichen Gitter, NoData/NaN,
halbe Zelle versetzt (geneigte Ebene), kleinere Referenz, CRS-/Höhenbezug-Konflikte,
Höhenbezug aus dem Namen, COG-Eigenschaften, Blockgrösse 37 = 2048, Überschreiben/alte Ausgabe,
Abbruch, swissSURFACE3D gemockt (Paging, Jahre, fehlende Kachel, Cache, Mosaik, LHN95-Abbruch),
STAC-Import gemockt (nie ein Item geschrieben, Payload, fehlendes Item, Area-Warnung, Multihash,
fehlgeschlagener Upload), Beschreibung gegen das echte DSM-RASTER-Asset, LineIDs wie GDWH-Import.
Im QGIS-Python werden die GUI-Tests übersprungen (kein tkinter), im System-Python die GDAL-Tests.
