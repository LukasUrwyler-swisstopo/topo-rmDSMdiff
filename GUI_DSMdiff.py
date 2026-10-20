"""
GUI_DSMdiff.py - DSM-DIFF: Differenzraster DSM aktuell - DSM alt als COG, optional direkt
als Asset <item>-dsm-diff.tif ins bestehende STAC-Item der Befliegung.

Tkinter, laeuft mit Standard-Python (nur tkinter + Standardbibliothek). Alle GDAL- und
Netzwerk-Arbeit macht process_scripts/_osgeo_runner.py im OSGeo4W/QGIS-Python als
Subprocess. Das GUI prueft nur Formales (Pfade, Endungen, LineID-Format).

Darstellung wie topo-GDWHimport/GUI_GDWHimport.py; daraus kopiert (Stand 2026-10-20):
Farbpaletten LIGHT/DARK, _apply_theme, Header mit Hell/Dark-Umschalter, Umgebungszeilen,
LineIDWidget, Terminal-Log, Start-Button amber/gruen, dunkle Titelleiste.
Aus topo-DMCdataConverter/GUI_DMCdataConverter.py kopiert (Stand 2026-10-20):
_detect_osgeo_python, _detect_python_home, Subprocess mit PROGRESS-Zeilen und Logdatei,
Fortschritt mit Restzeit, Datei-Info ueber die Runner-Aktion 'info'.
"""

import ctypes
import datetime
import glob as _glob
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# ─── Pfade ────────────────────────────────────────────────────────────────────
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
PROCESS_SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "process_scripts")
RUNNER_SCRIPT       = os.path.join(PROCESS_SCRIPTS_DIR, "_osgeo_runner.py")
CONFIG_FILE         = os.path.join(PROCESS_SCRIPTS_DIR, "_dsmdiff_config.json")
LOG_DIR             = os.path.join(SCRIPT_DIR, "logs")
DEFAULT_STAGING_DIR = r"Y:\02_DMC_tempProcessingFolder"
DEFAULT_SECRETS_DIR = os.path.join(SCRIPT_DIR, "secrets")

# Namen, Beschreibung, LineIDs - dieselben Module wie der Runner (nur Standardbibliothek)
if PROCESS_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, PROCESS_SCRIPTS_DIR)
import _line_ids
import stac_import

ALT_S3D  = "swisssurface3d"     # wie dsm_diff.ALT_S3D
ALT_FILE = "datei"              # wie dsm_diff.ALT_FILE
S3D_GSD_CM = 50
EXIT_OK, EXIT_ERROR, EXIT_CANCEL, EXIT_IMPORT_FAILED = 0, 1, 2, 3   # wie _osgeo_runner

AUFTRAGSTYP_LABELS = [f"{c} - {n}" for c, n in stac_import.AUFTRAGSTYPEN.items()]
DEFAULT_AUFTRAGSTYP_LABEL = next(l for l in AUFTRAGSTYP_LABELS
                                 if l.split(" - ", 1)[0] == stac_import.DEFAULT_AUFTRAGSTYP)

# DSM aus dem DMC-Converter: <JAHR>_<AREA>_DSM_<GSD>cm_LV95_<LN02|LHN95>.tif
DSM_NAME_PATTERN = re.compile(r"^(\d{4})_(.+?)_DSM_(\d+)cm_LV95_(LN02|LHN95)\.tiff?$",
                              re.IGNORECASE)


def parse_dsm_name(path):
    """{'jahr', 'area', 'gsd_cm', 'href'} aus einem DMC-DSM-Namen, sonst None."""
    m = DSM_NAME_PATTERN.match(os.path.basename(str(path or "").strip().strip('"')))
    if not m:
        return None
    return {"jahr": m.group(1), "area": m.group(2), "gsd_cm": int(m.group(3)),
            "href": m.group(4).upper()}


def diff_filename(jahr, area, gsd_cm):
    """Vorschlag lokaler Name, ohne Hoehenbezug (dh ist eine Differenz)."""
    return f"{jahr}_{area}_DSM-DIFF_{gsd_cm}cm_LV95.tif"


def mosaic_path_for(out_path, jahr, area):
    """swissSURFACE3D-Mosaik neben dem DIFF."""
    out_path = str(out_path or "").strip()
    if not out_path:
        return ""
    folder = os.path.dirname(out_path)
    if jahr and area:
        name = f"{jahr}_{area}_swissSURFACE3D_{S3D_GSD_CM}cm_LV95_LN02.tif"
    else:
        name = f"{Path(out_path).stem}_swissSURFACE3D_{S3D_GSD_CM}cm_LV95_LN02.tif"
    return os.path.join(folder, name)


def normalize_tif_path(path):
    path = str(path or "").strip().strip('"')
    if path and not path.lower().endswith((".tif", ".tiff")):
        path += ".tif"
    return path


def acquisition_year(line_ids, camera, jahr_text):
    """(jahr, quelle): aus dem Datum der ersten LineID, sonst aus dem Feld Jahr."""
    if line_ids:
        try:
            first = _line_ids.normalize(list(line_ids), camera)[0]
            return int(first[0:4]), "LineIDs"
        except (ValueError, IndexError):
            pass
    text = str(jahr_text or "").strip()
    if re.fullmatch(r"(19|20)\d{2}", text):
        return int(text), "Feld Jahr"
    return None, None


# ─── Konfiguration (OSGeo4W-Python, Staging, Secrets) ─────────────────────────
def _load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _save_config(**values):
    try:
        cfg = _load_config()
        cfg.update(values)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── OSGeo4W Python Erkennung (aus topo-DMCdataConverter) ─────────────────────
def _detect_osgeo_python() -> str:
    """Pfad zum OSGeo4W/QGIS-Python (aus Config, System-Python oder bekannten Pfaden)."""
    path = _load_config().get("osgeo_python", "")
    if path and os.path.isfile(path):
        return path
    try:
        if importlib.util.find_spec("osgeo") is not None:
            return sys.executable
    except Exception:
        pass
    kandidaten = []
    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        kandidaten.append(str(Path(osgeo_root) / "bin" / "python3.exe"))
    kandidaten += [r"C:\OSGeo4W\bin\python3.exe", r"C:\OSGeo4W64\bin\python3.exe"]
    for pat in [r"C:\Program Files\QGIS*\bin\python3.exe",
                r"C:\Program Files (x86)\QGIS*\bin\python3.exe"]:
        kandidaten.extend(sorted(_glob.glob(pat), reverse=True))
    return next((p for p in kandidaten if Path(p).is_file()), "")


def _detect_python_home(python_exe: str) -> str:
    """PYTHONHOME vom Python-Executable (QGIS: apps\\PythonXXX, OSGeo4W: root)."""
    bin_dir = os.path.dirname(python_exe)
    root_dir = os.path.dirname(bin_dir)
    apps_dir = os.path.join(root_dir, "apps")
    if os.path.isdir(apps_dir):
        for name in sorted(os.listdir(apps_dir), reverse=True):
            if name.lower().startswith("python"):
                candidate = os.path.join(apps_dir, name)
                if os.path.isdir(candidate):
                    return candidate
    return root_dir


def _runner_env(osgeo_python):
    env = os.environ.copy()
    env["PYTHONHOME"] = _detect_python_home(osgeo_python)
    # Private User-Site-Packages (z.B. eine per pip installierte NumPy) duerfen die
    # OSGeo4W-eigenen Pakete nicht ueberschatten (ABI-Konflikt, siehe topo-GDWHimport)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ─── Farbpaletten (aus topo-GDWHimport) ───────────────────────────────────────
LIGHT = {
    "root": "#f0f0f0", "panel": "#f5f5f5", "input": "#ffffff", "fg": "#1a1a1a",
    "fg_dim": "#666666", "accent": "#0063b1", "scroll": "#9098a8",
    "hdr_bg": "#1a3a5c", "hdr_fg": "#ffffff", "btn": "#e1e1e1", "btn_hover": "#c8c8c8",
    "list": "#ffffff", "log_bg": "#1e1e1e", "log_fg": "#d4d4d4", "sep": "#c0c0c0",
    "sel_bg": "#0078d4", "sel_fg": "#ffffff", "ok": "#2e7d32", "err": "#c62828",
    "hint": "#8a6f2e",
}
DARK = {
    "root": "#1e1e1e", "panel": "#252526", "input": "#3c3c3c", "fg": "#cccccc",
    "fg_dim": "#7a7a7a", "accent": "#4fc3f7", "scroll": "#6a6f7d",
    "hdr_bg": "#1a1a1a", "hdr_fg": "#cccccc", "btn": "#3c3c3c", "btn_hover": "#505050",
    "list": "#2d2d30", "log_bg": "#1e1e1e", "log_fg": "#d4d4d4", "sep": "#3c3c3c",
    "sel_bg": "#094771", "sel_fg": "#cccccc", "ok": "#66bb6a", "err": "#ef5350",
    "hint": "#c9a84c",
}


def _set_titlebar_dark(win, dark):
    """Windows-Titelleiste im Darkmode dunkel (DWM); Fenster muss sichtbar sein."""
    if not win.winfo_ismapped():
        win.after(50, lambda: _set_titlebar_dark(win, dark))
        return
    try:
        hwnd = int(win.wm_frame(), 16)
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # 20 = Win11 / Win10 2004+, 19 = aeltere Builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    except Exception:
        pass


# ─── LineID-Listbox-Widget (aus topo-GDWHimport, ohne SB_DOP_16-Limit) ───────
class LineIDWidget(ttk.LabelFrame):
    def __init__(self, parent, label, comment="", camera_getter=None, **kw):
        super().__init__(parent, text=label, padding=6, **kw)
        self._comment_text = comment
        self._comment_lbl = None
        self._camera_getter = camera_getter
        self._build()

    def _camera(self):
        return self._camera_getter() if self._camera_getter else ""

    def _fmt_text(self):
        return (f" Format: {_line_ids.format_hint(self._camera())}"
                "  |  Mehrere IDs: aus Excel einfügen (Ctrl+V)")

    def _build(self):
        if self._comment_text:
            self._comment_lbl = ttk.Label(self, text=self._comment_text,
                                          font=("Segoe UI", 8, "italic"))
            self._comment_lbl.pack(fill="x", pady=(0, 3))
        ef = ttk.Frame(self)
        ef.pack(fill="x")
        self.var = tk.StringVar()
        self._entry = ttk.Entry(ef, textvariable=self.var, width=32)
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<Return>", lambda _: self._add())
        self._entry.bind("<Control-v>", self._on_paste)
        self._entry.bind("<Control-V>", self._on_paste)
        ttk.Button(ef, text="  +  ", command=self._add).pack(side="left", padx=(6, 0))
        self._fmt_lbl = ttk.Label(self, text=self._fmt_text(), font=("", 8))
        self._fmt_lbl.pack(fill="x", pady=(2, 0))
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, pady=(4, 0))
        sb = ttk.Scrollbar(lf, orient="vertical")
        self.lb = tk.Listbox(lf, height=4, yscrollcommand=sb.set, selectmode="extended",
                             font=("Courier New", 9), activestyle="none", relief="flat",
                             borderwidth=1)
        sb.config(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        ttk.Button(self, text="Ausgewählte entfernen", command=self._remove
                   ).pack(anchor="e", pady=(3, 0))

    def refresh_camera(self):
        """Nach einem CameraSystem-Wechsel: unpassende IDs entfernen (ADS <-> DMC-4)."""
        self._fmt_lbl.config(text=self._fmt_text())
        cam = self._camera()
        ids = list(self.lb.get(0, "end"))
        removed = [i for i in ids if not _line_ids.is_valid(i, cam)]
        if removed:
            self.lb.delete(0, "end")
            for i in ids:
                if i not in removed:
                    self.lb.insert("end", i)
        self._resort()
        return removed

    def _existing_line_keys(self):
        cam = self._camera()
        return {_line_ids.line_key(i, cam) for i in self.lb.get(0, "end")
                if _line_ids.is_valid(i, cam)}

    def _existing_area_key(self):
        cam = self._camera()
        for i in self.lb.get(0, "end"):
            if _line_ids.is_valid(i, cam):
                return _line_ids.area_key(i, cam)
        return None

    def add_ids(self, lines):
        """Fuegt LineIDs hinzu (Format, Duplikat, Area-Gruppe geprueft).
        Gibt (hinzugefuegt, uebersprungen-mit-Grund) zurueck."""
        cam = self._camera()
        added, skipped = [], []
        existing = self._existing_line_keys()
        expected_area = self._existing_area_key()
        for line in lines:
            if not _line_ids.is_valid(line, cam):
                skipped.append(f"  {line}  →  falsches Format")
            elif _line_ids.line_key(line, cam) in existing:
                skipped.append(f"  {line}  →  Duplikat (Linie bereits erfasst)")
            elif expected_area is not None and _line_ids.area_key(line, cam) != expected_area:
                skipped.append(f"  {line}  →  andere Area (erwartet {expected_area}_HHMMSS)")
            else:
                self.lb.insert("end", line)
                existing.add(_line_ids.line_key(line, cam))
                if expected_area is None:
                    expected_area = _line_ids.area_key(line, cam)
                added.append(line)
        if added:
            self._resort()
        return added, skipped

    def _add(self):
        val = self.var.get().strip()
        if not val:
            return
        added, skipped = self.add_ids([val])
        if added:
            self.var.set("")
        else:
            cam = self._camera()
            messagebox.showwarning(
                "LineID nicht übernommen",
                f"{skipped[0].strip()}\n\nErwartet: {_line_ids.format_hint(cam)}\n"
                f"Beispiel:  {_line_ids.example(cam)}", parent=self)

    def _on_paste(self, _=None):
        """Mehrfacheingabe aus Excel (eine ID pro Zeile)."""
        try:
            clipboard = self._entry.clipboard_get()
        except Exception:
            return
        lines = [l.strip() for l in clipboard.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if l.strip()]
        if len(lines) <= 1:
            return   # einzelne Zeile -> normales Einfuegen
        self.var.set("")
        added, skipped = self.add_ids(lines)
        if skipped:
            messagebox.showwarning(
                "Mehrfacheingabe – Warnung",
                f"{len(added)} ID(s) hinzugefügt.\n\nNicht importiert ({len(skipped)}):\n"
                + "\n".join(skipped[:15]) + ("\n…" if len(skipped) > 15 else ""), parent=self)
        return "break"

    def _remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def _resort(self):
        """Chronologisch, aelteste zuoberst (siehe _line_ids.sort_key)."""
        cam = self._camera()
        try:
            ids = sorted(self.lb.get(0, "end"), key=lambda i: _line_ids.sort_key(i, cam))
        except (ValueError, IndexError):
            ids = sorted(self.lb.get(0, "end"))
        self.lb.delete(0, "end")
        for i in ids:
            self.lb.insert("end", i)

    def get_ids(self):
        return list(self.lb.get(0, "end"))

    def clear(self):
        self.lb.delete(0, "end")

    def apply_theme(self, T):
        self.lb.config(bg=T["list"], fg=T["fg"], selectbackground=T["sel_bg"],
                       selectforeground=T["sel_fg"], highlightbackground=T["sep"],
                       highlightcolor=T["sep"])
        self._fmt_lbl.config(foreground=T["fg_dim"])
        if self._comment_lbl:
            self._comment_lbl.config(foreground=T["fg_dim"])


# ─── Bestaetigung PROD (Stil wie SicherheitsCheckDialog in topo-GDWHimport) ───
class ProdConfirmDialog(tk.Toplevel):
    """Modal: PROD ist oeffentlich - Item-ID und Asset-Name ausdruecklich bestaetigen."""

    def __init__(self, parent, item, asset, diff_path, dark=True):
        super().__init__(parent)
        self.result = False
        T = DARK if dark else LIGHT
        self._T = T
        self.title("STAC-Import nach PROD bestätigen")
        self.resizable(False, False)
        self.configure(bg=T["root"])
        self.transient(parent)
        self.grab_set()

        hdr = tk.Frame(self, bg=T["hdr_bg"])
        hdr.pack(fill="x")
        tk.Label(hdr, text="  STAC-Import nach PROD  –  öffentlich sichtbar",
                 font=("Segoe UI", 11, "bold"), bg=T["hdr_bg"], fg=T["hdr_fg"]
                 ).pack(side="left", pady=10)

        body = tk.Frame(self, bg=T["root"])
        body.pack(fill="both", expand=True, padx=14, pady=10)
        rows = [("Umgebung:", f"PROD  ({stac_import.STAC_HOSTS['PROD']})"),
                ("Item-ID:", item), ("Asset-Name:", asset),
                ("Asset-Titel:", stac_import.ASSET_TITLE),
                ("Browser-Link:", stac_import.browser_link(item, "PROD")),
                ("Item:", "wird angelegt, falls es fehlt (Geometrie = Perimeter DSM aktuell, "
                          "datetime = erste LineID); ein bestehendes Item bleibt unverändert"),
                ("DIFF-Datei:", diff_path)]
        for key, val in rows:
            row = tk.Frame(body, bg=T["root"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=key, width=14, anchor="nw", font=("Segoe UI", 9),
                     bg=T["root"], fg=T["fg_dim"]).pack(side="left", anchor="n")
            tk.Label(row, text=val, anchor="nw", font=("Segoe UI", 9, "bold"), bg=T["root"],
                     fg=T["fg"], wraplength=480, justify="left").pack(side="left", fill="x")

        box_bg, box_fg = ("#3e2a00", "#ffe082") if dark else ("#fff8e1", "#e65100")
        box = tk.Frame(body, bg=box_bg, padx=12, pady=10)
        box.pack(fill="x", pady=(10, 0))
        tk.Label(box, text="Ein Asset im falschen Item ist sofort öffentlich. Item-ID und "
                           "Asset-Name kontrollieren.", font=("Segoe UI", 10, "bold"),
                 bg=box_bg, fg=box_fg, wraplength=500, justify="left").pack(anchor="w")
        self._ok_var = tk.BooleanVar(value=False)
        tk.Checkbutton(box, text="  Ja – Item-ID und Asset-Name kontrolliert und korrekt",
                       variable=self._ok_var, font=("Segoe UI", 10, "bold"), bg=box_bg,
                       fg=box_fg, activebackground=box_bg, activeforeground=box_fg,
                       selectcolor=box_bg, command=self._on_toggle).pack(anchor="w", pady=(6, 0))

        tk.Frame(self, height=1, bg=T["sep"]).pack(fill="x")
        btn_row = tk.Frame(self, bg=T["root"])
        btn_row.pack(fill="x", padx=14, pady=10)
        self._go_btn = tk.Button(btn_row, text="▶   nach PROD importieren",
                                 font=("Segoe UI", 10, "bold"), bg=T["btn"], fg=T["fg_dim"],
                                 activebackground=T["sel_bg"], activeforeground="#ffffff",
                                 relief="flat", padx=18, pady=7, cursor="hand2",
                                 state="disabled", command=self._confirm)
        self._go_btn.pack(side="right")
        tk.Button(btn_row, text="Abbrechen", font=("Segoe UI", 10), bg=T["btn"], fg=T["fg"],
                  activebackground=T["btn_hover"], activeforeground=T["fg"], relief="flat",
                  padx=14, pady=7, cursor="hand2", command=self.destroy
                  ).pack(side="right", padx=(0, 10))

        self.update_idletasks()
        w, h = 620, self.winfo_reqheight()
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - h // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        _set_titlebar_dark(self, dark)
        self.bind("<Escape>", lambda _: self.destroy())

    def _on_toggle(self):
        if self._ok_var.get():
            self._go_btn.config(state="normal", bg="#005fa3", fg="#ffffff")
        else:
            self._go_btn.config(state="disabled", bg=self._T["btn"], fg=self._T["fg_dim"])

    def _confirm(self):
        self.result = True
        self.destroy()

    def wait(self):
        self.wait_window()
        return self.result


# ─── Haupt-App ────────────────────────────────────────────────────────────────
class DSMDiffApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DSM-DIFF")
        screen_h = self.winfo_screenheight()
        win_h = min(1060, screen_h - 80)
        self.geometry(f"900x{win_h}")
        self.minsize(760, min(760, win_h))
        self.resizable(True, True)

        cfg = _load_config()
        self._dark = False
        self._running = False
        self._log_q = queue.Queue()
        self._log_file = None
        self._error_lines = []
        self._log_visible = False
        self._dim_labels, self._accent_labels = [], []
        self._hint_labels, self._err_labels = [], []
        self._osgeo_python = _detect_osgeo_python()
        self._staging_dir = cfg.get("staging_dir") or DEFAULT_STAGING_DIR
        self._cancel_file = None
        self._close_after_run = False
        self._active_vorgang = ""
        self._pending_link = None
        self._s3d_result = None      # letztes Ergebnis 'Kacheln suchen'
        self._s3d_key = None         # Eingaben, fuer die es gilt
        self._out_auto = ""          # zuletzt vorgeschlagener Ausgabepfad
        self._preview_sig = None
        self._progress_start = None

        self._build_ui(cfg)
        self._apply_theme(True)      # Dark Mode als Standard (wie GDWH-Import)
        self.after(120, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Aufbau ──────────────────────────────────────────────────────────────
    def _build_ui(self, cfg):
        self._hdr = tk.Frame(self, height=52)
        self._hdr.pack(fill="x")
        self._hdr.pack_propagate(False)
        self._hdr_lbl = tk.Label(self._hdr, text="DSM-DIFF", font=("Segoe UI", 16, "bold"))
        self._hdr_lbl.pack(side="left", padx=16, pady=12)
        self._hdr_sub = tk.Label(self._hdr, text="DSM aktuell − DSM alt  →  COG  (+ STAC-Asset)",
                                 font=("Segoe UI", 9))
        self._hdr_sub.pack(side="left", pady=(16, 10))
        self._theme_btn = tk.Button(self._hdr, text="Dark", command=self._toggle_theme,
                                    relief="flat", borderwidth=0, font=("", 9),
                                    cursor="hand2", padx=10, pady=4)
        self._theme_btn.pack(side="right", padx=12)

        # Umgebung - dezent ganz oben, einmal eingerichtet selten geaendert
        f = ttk.Frame(self)
        f.pack(fill="x", padx=12, pady=(6, 0))
        lbl = ttk.Label(f, text="OSGeo4W Python:", font=("", 8))
        lbl.pack(side="left")
        self._dim_labels.append(lbl)
        self._osgeo_lbl = ttk.Label(f, font=("Courier New", 8))
        self._osgeo_lbl.pack(side="left", padx=(6, 0))
        self._osgeo_status = ttk.Label(f, font=("", 8, "bold"))
        self._osgeo_status.pack(side="left", padx=(6, 0))
        ttk.Button(f, text="Ändern…", style="Small.TButton",
                   command=self._set_osgeo_python).pack(side="right")

        f = ttk.Frame(self)
        f.pack(fill="x", padx=12, pady=(2, 0))
        lbl = ttk.Label(f, text="Staging-Ordner:", font=("", 8))
        lbl.pack(side="left")
        self._dim_labels.append(lbl)
        self._staging_lbl = ttk.Label(f, font=("Courier New", 8))
        self._staging_lbl.pack(side="left", padx=(6, 0))
        self._staging_status = ttk.Label(f, font=("", 8, "bold"))
        self._staging_status.pack(side="left", padx=(6, 0))
        ttk.Button(f, text="Ändern…", style="Small.TButton",
                   command=self._set_staging_dir).pack(side="right")
        self.keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Staging behalten", variable=self.keep_staging_var
                        ).pack(side="right", padx=(0, 10))

        # Scrollbarer Formular-Bereich
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=12, pady=6)
        self._canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._sf = ttk.Frame(self._canvas)
        win_id = self._canvas.create_window((0, 0), window=self._sf, anchor="nw")
        self._sf.bind("<Configure>",
                      lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(win_id, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel)
        # Mausrad ueber einer Combobox scrollt das Formular, statt die Auswahl zu verstellen
        self.bind_class("TCombobox", "<MouseWheel>", self._on_wheel)

        self._build_neu(self._sf)
        self._build_alt(self._sf)
        self._build_meta(self._sf)
        self._build_output(self._sf)
        self._build_stac(self._sf, cfg)

        # Log (initial versteckt, beim Start eingeblendet - Terminal-Button wie GDWH-Import)
        self._log_sep = ttk.Separator(self)
        self._log_frame = ttk.LabelFrame(self, text="Log-Ausgabe", padding=4,
                                         style="Section.TLabelframe")
        self.log_box = scrolledtext.ScrolledText(self._log_frame, height=11, wrap="word",
                                                 state="disabled", font=("Courier New", 9))
        self.log_box.pack(fill="both", expand=True)

        self._progress_frame = ttk.Frame(self)
        self._progress_bar = ttk.Progressbar(self._progress_frame, mode="indeterminate")
        self._progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._progress_lbl = ttk.Label(self._progress_frame, text="Verarbeitung läuft…",
                                       font=("", 9))
        self._progress_lbl.pack(side="left")

        self._btn_row = ttk.Frame(self)
        self._btn_row.pack(fill="x", padx=12, pady=(0, 10))
        self.start_btn = tk.Button(self._btn_row, text="▶   DSM-DIFF BERECHNEN",
                                   font=("Segoe UI", 10, "bold"), relief="flat",
                                   cursor="hand2", padx=22, pady=7, command=self._start)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(self._btn_row, text="Abbrechen", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 10))
        ttk.Button(self._btn_row, text="Log löschen", command=self._clear_log
                   ).pack(side="right", padx=(0, 10))
        self._terminal_btn = ttk.Button(self._btn_row, text="Terminal ▾", command=self._toggle_log)
        self._terminal_btn.pack(side="left")

        self._update_osgeo_label()
        self._update_staging_label()
        self._on_alt_change()

    def _section(self, parent, title):
        sec = ttk.LabelFrame(parent, text=title, padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)
        return sec

    def _key(self, parent, text, row, sticky="w", pady=3):
        lbl = ttk.Label(parent, text=text, font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky=sticky, pady=pady)
        return lbl

    def _hint(self, parent, text, row, col=1, colspan=2, kind="dim"):
        lbl = ttk.Label(parent, text=text, font=("", 8), justify="left")
        lbl.grid(row=row, column=col, columnspan=colspan, sticky="w", padx=(8, 0))
        {"dim": self._dim_labels, "hint": self._hint_labels,
         "err": self._err_labels, "accent": self._accent_labels}[kind].append(lbl)
        return lbl

    def _path_row(self, parent, row, var, button_text, command):
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew",
                                                 padx=(8, 4), pady=3)
        btn = ttk.Button(parent, text=button_text, command=command)
        btn.grid(row=row, column=2, sticky="ew", pady=3)
        return btn

    def _build_neu(self, parent):
        sec = self._section(parent, "DSM aktuell")
        self._key(sec, "Datei oder STAC-Link:", 0)
        self.neu_var = tk.StringVar()
        self._path_row(sec, 0, self.neu_var, "Datei…", self._browse_neu)
        entry = sec.grid_slaves(row=0, column=1)[0]
        for seq in ("<FocusOut>", "<Return>", "<KP_Enter>"):
            entry.bind(seq, lambda *_: self._on_neu_changed())
        entry.bind("<<Paste>>", lambda *_: self.after_idle(self._on_neu_changed))
        self.neu_info_lbl = self._hint(sec, "–", 1, kind="accent")
        self.neu_problem_lbl = self._hint(sec, "", 2, kind="err")
        self._hint(sec, "Lokal (GeoTIFF/COG + TFW): Name <JAHR>_<AREA>_DSM_<GSD>cm_LV95_<LN02|LHN95>.tif "
                        "belegt Jahr, Area und Ausgabenamen vor.\n"
                        "STAC-Link (href eines Assets auf data.geo.admin.ch / sys-data.int.bgdi.ch): "
                        "Auftragstyp, Area, CameraSystem, LineIDs und Jahr kommen aus den STAC-\n"
                        "Metadaten; die Datei wird beim Lauf ins Staging geladen (Prüfsumme).", 3)

    def _build_alt(self, parent):
        sec = self._section(parent, "DSM alt (Referenz)")
        self.alt_mode_var = tk.StringVar(value=ALT_S3D)
        ttk.Radiobutton(sec, text="swissSURFACE3D Raster aus dem swisstopo-STAC  "
                                  "(Kacheln, die DSM aktuell überlappen – automatisch gesucht und geladen)",
                        variable=self.alt_mode_var, value=ALT_S3D, command=self._on_alt_change
                        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self._s3d_frame = ttk.Frame(sec)
        self._s3d_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(22, 0), pady=(2, 8))
        self._s3d_frame.columnconfigure(1, weight=1)
        self._key(self._s3d_frame, "Jahr je Kachel:", 0)
        self.s3d_rule_lbl = ttk.Label(self._s3d_frame, text="–", font=("", 9))
        self.s3d_rule_lbl.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._accent_labels.append(self.s3d_rule_lbl)
        self.s3d_search_btn = ttk.Button(self._s3d_frame, text="Kacheln suchen",
                                         command=self._search_tiles)
        self.s3d_search_btn.grid(row=2, column=0, sticky="nw", pady=(6, 0))
        self.s3d_result_lbl = ttk.Label(self._s3d_frame, text="(noch nicht gesucht – der Lauf "
                                                              "sucht ohnehin selbst)",
                                        font=("", 8), justify="left", wraplength=620)
        self.s3d_result_lbl.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))
        self._dim_labels.append(self.s3d_result_lbl)
        h = ttk.Label(self._s3d_frame, font=("", 8), justify="left",
                      text="Höhenbezug: LN02 laut STAC-Assetname _2056_5728 (bei allen Kacheln "
                           "geprüft, CRS-Tag einer Kachel zusätzlich) – LHN95 → Abbruch.\n"
                           "Mosaik (Merged TIFF) als COG neben dem DIFF, Originalgitter 0.5 m, "
                           "NoData -3.4028235e+38, EPSG:2056+5728.")
        h.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._dim_labels.append(h)

        ttk.Radiobutton(sec, text="Eigenes DSM (Pfad oder STAC-Link)", variable=self.alt_mode_var,
                        value=ALT_FILE, command=self._on_alt_change
                        ).grid(row=2, column=0, columnspan=3, sticky="w")
        self._file_frame = ttk.Frame(sec)
        self._file_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=(22, 0), pady=(2, 0))
        self._file_frame.columnconfigure(1, weight=1)
        self._key(self._file_frame, "Datei oder STAC-Link:", 0)
        self.alt_var = tk.StringVar()
        self._path_row(self._file_frame, 0, self.alt_var, "Datei…", self._browse_alt)
        entry = self._file_frame.grid_slaves(row=0, column=1)[0]
        for seq in ("<FocusOut>", "<Return>", "<KP_Enter>"):
            entry.bind(seq, lambda *_: self._refresh_alt_info())
        entry.bind("<<Paste>>", lambda *_: self.after_idle(self._refresh_alt_info))
        self.alt_info_lbl = self._hint(self._file_frame, "–", 1, kind="accent")
        self.alt_problem_lbl = self._hint(self._file_frame, "", 2, kind="err")
        self._hint(self._file_frame, "Ein Band, sonst gleiche Prüfungen wie DSM aktuell.\n"
                                     "Höhenbezug aus CRS-Tag, STAC-Beschreibung oder Dateiname "
                                     "(_LV95_LN02) – muss zu DSM aktuell passen.\n"
                                     "Bei einem STAC-Link werden TerrainModel 2 und dessen Jahr "
                                     "vorbelegt.", 3)

    def _build_meta(self, parent):
        sec = self._section(parent, "Meta-Informationen / STAC")
        r = 0
        self._key(sec, "Auftragstyp:", r)
        self.auftragstyp_var = tk.StringVar(value=DEFAULT_AUFTRAGSTYP_LABEL)
        ttk.Combobox(sec, textvariable=self.auftragstyp_var, values=AUFTRAGSTYP_LABELS,
                     state="readonly", width=30).grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        r += 1
        self._key(sec, "CameraSystem:", r)
        self.camera_var = tk.StringVar(value=stac_import.DEFAULT_CAMERA)
        ttk.Combobox(sec, textvariable=self.camera_var, values=stac_import.CAMERA_SYSTEMS,
                     state="readonly", width=20).grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        self.camera_var.trace_add("write", lambda *_: self._on_camera_change())
        r += 1
        self._key(sec, "Area:", r)
        self.area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self.area_var, width=28).grid(row=r, column=1, sticky="w",
                                                                  padx=(8, 0), pady=3)
        self._hint(sec, "aus dem Dateinamen von DSM aktuell vorbelegt [editierbar]", r + 1)
        r += 2
        self._key(sec, "Jahr:", r)
        self.jahr_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self.jahr_var, width=8).grid(row=r, column=1, sticky="w",
                                                                 padx=(8, 0), pady=3)
        self._hint(sec, "Aufnahmejahr (Ausgabenamen; Jahreswahl swissSURFACE3D, wenn keine LineIDs)",
                   r + 1)
        r += 2
        self._key(sec, "TerrainModel 1:", r)
        self.tm1_var = tk.StringVar(value=stac_import.TERRAIN_MODEL_1_DEFAULT)
        ttk.Combobox(sec, textvariable=self.tm1_var, values=stac_import.TERRAIN_MODELS,
                     state="readonly", width=60).grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        r += 1
        self._key(sec, "TerrainModel 2:", r)
        tm2 = ttk.Frame(sec)
        tm2.grid(row=r, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        self.tm2_s3d_lbl = ttk.Label(tm2, text=f"{stac_import.TERRAIN_MODEL_S3D}  (+ Jahre der Kacheln)",
                                     font=("", 9, "bold"))
        self._accent_labels.append(self.tm2_s3d_lbl)
        self._tm2_file_frame = ttk.Frame(tm2)
        self.tm2_var = tk.StringVar(value=stac_import.TERRAIN_MODELS[0])
        ttk.Combobox(self._tm2_file_frame, textvariable=self.tm2_var,
                     values=stac_import.TERRAIN_MODELS, state="readonly", width=60).pack(side="left")
        ttk.Label(self._tm2_file_frame, text="  Jahr:").pack(side="left")
        self.tm2_year_var = tk.StringVar()
        ttk.Entry(self._tm2_file_frame, textvariable=self.tm2_year_var, width=8).pack(side="left")
        r += 1
        self._key(sec, "SourceRefSys:", r)
        srs_row = ttk.Frame(sec)
        srs_row.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        v = ttk.Label(srs_row, text=stac_import.SOURCE_REF_SYS, font=("", 9, "bold"))
        v.pack(side="left")
        s = ttk.Label(srs_row, text="  [Standard]", font=("", 8))
        s.pack(side="left")
        self._accent_labels.append(v)
        self._dim_labels.append(s)
        r += 1
        self._key(sec, "Commentary:", r)
        self.commentary_var = tk.StringVar(value=stac_import.DEFAULT_COMMENTARY)
        ttk.Entry(sec, textvariable=self.commentary_var).grid(row=r, column=1, sticky="ew",
                                                              padx=(8, 0), pady=3)
        r += 1
        self.lineid_w = LineIDWidget(
            sec, "LineIDs",
            comment="alle LineIDs des AOI – werden chronologisch sortiert, die oberste ist die "
                    "erste beflogene Linie (bestimmt die Item-ID)",
            camera_getter=self.camera_var.get)
        self.lineid_w.grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        r += 1

        pv = ttk.LabelFrame(sec, text="Vorschau STAC", padding=8)
        pv.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        pv.columnconfigure(1, weight=1)
        self._preview = {}
        for i, (key, label) in enumerate((("dt", "StacItemIdDatetime:"), ("item", "Item-ID:"),
                                          ("asset", "Asset-Name:"), ("title", "Asset-Titel:"),
                                          ("link", "Browser-Link:"), ("desc", "Beschreibung:"))):
            k = ttk.Label(pv, text=label, font=("Segoe UI", 9))
            k.grid(row=i, column=0, sticky="nw", pady=1)
            self._dim_labels.append(k)
            val = ttk.Label(pv, text="–", font=("Consolas", 9), justify="left", wraplength=640)
            val.grid(row=i, column=1, sticky="w", padx=(8, 0), pady=1)
            self._accent_labels.append(val)
            self._preview[key] = val
        self._preview["link"].configure(cursor="hand2")
        self._preview["link"].bind("<Button-1>", lambda _e: self._open_link())

    def _build_output(self, parent):
        sec = self._section(parent, "Ausgabe")
        self._key(sec, "DIFF-COG (.tif):", 0)
        self.out_var = tk.StringVar()
        self._path_row(sec, 0, self.out_var, "Datei…", self._browse_out)
        self._hint(sec, "Vorschlag <JAHR>_<AREA>_DSM-DIFF_<GSD>cm_LV95.tif im Ordner von DSM aktuell "
                        "– der STAC-Asset-Name ist davon unabhängig.", 1)
        self._key(sec, "Mosaik swissSURFACE3D:", 2)
        self.mosaic_lbl = ttk.Label(sec, text="–", font=("Courier New", 8))
        self.mosaic_lbl.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(self.mosaic_lbl)
        self._hint(sec, "Gleichnamige Dateien werden ersetzt – erst nach bestandener Prüfung;\n"
                        "schlägt der Lauf fehl, bleibt die alte Datei unverändert.", 3)

    def _build_stac(self, parent, cfg):
        sec = self._section(parent, "STAC-Import")
        self.stac_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="direkter STAC-Import nach der Berechnung (Asset ins Item der Befliegung)",
                        variable=self.stac_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self._key(sec, "Umgebung:", 1)
        env_row = ttk.Frame(sec)
        env_row.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        self.env_var = tk.StringVar(value=stac_import.DEFAULT_ENV)     # INT ist immer Default
        ttk.Radiobutton(env_row, text=f"INT  ({stac_import.STAC_HOSTS['INT']})",
                        variable=self.env_var, value="INT").pack(side="left")
        ttk.Radiobutton(env_row, text=f"PROD  ({stac_import.STAC_HOSTS['PROD']})",
                        variable=self.env_var, value="PROD").pack(side="left", padx=(16, 0))
        self._hint(sec, "INT ist Standard. PROD nur nach Bestätigungsdialog mit Item-ID und Asset-Name.",
                   2, kind="hint")
        self._key(sec, "Secrets-Ordner:", 3)
        self.secrets_var = tk.StringVar(value=cfg.get("secrets_dir") or DEFAULT_SECRETS_DIR)
        self._path_row(sec, 3, self.secrets_var, "Ordner…", self._browse_secrets)
        self._hint(sec, "stac_credentials.json + proxy_config.json (Vorlagen in proxy_credentials\\) "
                        "– Inhalt wird nie angezeigt oder geloggt.", 4)
        self.replace_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="vorhandenes STAC-Asset ersetzen  (ohne Haken: Abbruch vor der "
                                  "Rechnung, wenn es das Asset schon gibt)",
                        variable=self.replace_var).grid(row=5, column=0, columnspan=3,
                                                        sticky="w", pady=(6, 0))
        self._hint(sec, "Fehlt das Item, wird es angelegt (Geometrie = Perimeter von DSM aktuell, "
                        "datetime = erste LineID) – DSM-RASTER und DOP folgen später via GDWH.\n"
                        "Ein bestehendes Item wird nie verändert, angelegt wird dann nur das Asset.",
                   6, col=0, colspan=3)

        # Import eines schon gerechneten DIFF (z.B. wenn der Import beim Lauf scheiterte) -
        # eigene Zeile, damit das Feld 'Ausgabe' reine Ausgabe bleibt
        ttk.Separator(sec, orient="horizontal").grid(row=7, column=0, columnspan=3, sticky="ew",
                                                     pady=(10, 6))
        self._key(sec, "Vorhandenes DIFF-COG:", 8)
        self.import_diff_var = tk.StringVar()
        self._path_row(sec, 8, self.import_diff_var, "Datei…", self._browse_import_diff)
        entry = sec.grid_slaves(row=8, column=1)[0]
        for seq in ("<FocusOut>", "<Return>", "<KP_Enter>"):
            entry.bind(seq, lambda *_: self._refresh_import_diff_info())
        entry.bind("<<Paste>>", lambda *_: self.after_idle(self._refresh_import_diff_info))
        self.import_diff_info_lbl = self._hint(sec, "–", 9, kind="accent")
        self.import_diff_problem_lbl = self._hint(sec, "", 10, kind="err")
        imp_row = ttk.Frame(sec)
        imp_row.grid(row=11, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(4, 0))
        self.import_btn = ttk.Button(imp_row, text="▶  Nur STAC-Import dieses DIFF",
                                     command=self._start_import_only)
        self.import_btn.pack(side="left")
        h = ttk.Label(imp_row, font=("", 8), justify="left",
                      text="  ohne neue Rechnung; Area, LineIDs und Auftragstyp kommen aus dem DIFF,\n"
                           "  Umgebung, Secrets und 'ersetzen' von oben. Nach einem Lauf steht "
                           "das neue DIFF schon hier.")
        h.pack(side="left")
        self._dim_labels.append(h)

    # ── Mausrad ────────────────────────────────────────────────────────────────
    def _on_wheel(self, event):
        """Formular scrollen - nicht, wenn Log oder LineID-Liste selbst scrollen."""
        cls = event.widget.winfo_class() if hasattr(event.widget, "winfo_class") else ""
        if cls in ("Text", "Listbox"):
            return None
        try:
            if str(event.widget.winfo_toplevel()) != str(self):
                return None
        except Exception:
            return None
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    # ── Datei-Dialoge ──────────────────────────────────────────────────────────
    def _ask_tif(self, var, title):
        cur = var.get().strip().strip('"')
        kw = {"title": title, "filetypes": [("GeoTIFF / COG", "*.tif *.tiff"), ("Alle Dateien", "*.*")]}
        if cur and os.path.isdir(os.path.dirname(cur)):
            kw["initialdir"] = os.path.dirname(cur)
        path = filedialog.askopenfilename(**kw)
        if path:
            var.set(path.replace("/", "\\"))
        return bool(path)

    def _browse_neu(self):
        if self._ask_tif(self.neu_var, "DSM aktuell auswählen"):
            self._on_neu_changed()

    def _browse_alt(self):
        if self._ask_tif(self.alt_var, "DSM alt auswählen"):
            self._refresh_alt_info()

    def _suggested_out_name(self):
        """Vorschlag <JAHR>_<AREA>_DSM-DIFF_<GSD>cm_LV95.tif aus den Feldern (STAC-Link: GSD
        aus den Metadaten)."""
        jahr, area = self.jahr_var.get().strip(), self.area_var.get().strip()
        gsd = getattr(self, "_neu_gsd_m", None)
        if jahr and area and gsd:
            return diff_filename(jahr, area, round(float(gsd) * 100))
        return ""

    def _browse_out(self):
        cur = self.out_var.get().strip()
        neu = self.neu_var.get().strip()
        if not cur and not stac_import.is_url(neu):
            cur = neu
        kw = {"title": "DIFF-COG festlegen", "defaultextension": ".tif",
              "filetypes": [("GeoTIFF", "*.tif"), ("Alle Dateien", "*.*")]}
        if cur and os.path.isdir(os.path.dirname(cur)):
            kw["initialdir"] = os.path.dirname(cur)
        name = os.path.basename(self.out_var.get().strip()) or self._suggested_out_name()
        if name:
            kw["initialfile"] = name
        path = filedialog.asksaveasfilename(**kw)
        if path:
            self.out_var.set(normalize_tif_path(path.replace("/", "\\")))

    def _browse_import_diff(self):
        if self._ask_tif(self.import_diff_var, "Vorhandenes DIFF-COG für den STAC-Import auswählen"):
            self._refresh_import_diff_info()

    def _refresh_import_diff_info(self):
        self._refresh_info(self.import_diff_var, self.import_diff_info_lbl,
                           self.import_diff_problem_lbl, on_diff=self._apply_diff_metadata)

    def _apply_diff_metadata(self, md):
        """Area, LineIDs, Auftragstyp, CameraSystem aus den DSMDIFF_*-Metadaten eines DIFF."""
        if not md.get("DSMDIFF_DEFINITION"):
            self.import_diff_problem_lbl.configure(
                text="⚠ keine DSMDIFF-Metadaten – nicht mit diesem Werkzeug erstellt?")
            return
        filled = []
        code = stac_import.auftragstyp_from_item(md.get("DSMDIFF_STAC_ITEM"))
        if code:
            self.auftragstyp_var.set(next(l for l in AUFTRAGSTYP_LABELS
                                          if l.split(" - ", 1)[0] == code))
            filled.append(f"Auftragstyp {code}")
        if md.get("DSMDIFF_AREA"):
            self.area_var.set(md["DSMDIFF_AREA"])
            filled.append(f"Area {md['DSMDIFF_AREA']}")
        ids = [l for l in (md.get("DSMDIFF_LINE_IDS") or "").split(",") if l.strip()]
        if ids:
            self.lineid_w.clear()
        if md.get("DSMDIFF_CAMERA") in stac_import.CAMERA_SYSTEMS:
            self.camera_var.set(md["DSMDIFF_CAMERA"])
            filled.append(f"CameraSystem {md['DSMDIFF_CAMERA']}")
        if ids:
            added, _ = self.lineid_w.add_ids(ids)
            filled.append(f"{len(added)} LineID(s)")
        if md.get("DSMDIFF_SOURCE_OLD_TYPE") == "file":
            self.alt_mode_var.set(ALT_FILE)          # TerrainModel 2 kommt dann aus dem GUI
            self._on_alt_change()
        self._preview_sig = None
        if filled:
            self._log(f"Vorbelegt aus dem DIFF ({os.path.basename(self.import_diff_var.get())}): "
                      f"{', '.join(filled)}\n")

    def _browse_secrets(self):
        cur = self.secrets_var.get().strip()
        kw = {"title": "Secrets-Ordner auswählen"}
        if cur and os.path.isdir(cur):
            kw["initialdir"] = cur
        path = filedialog.askdirectory(**kw)
        if path:
            path = path.replace("/", "\\")
            self.secrets_var.set(path)
            _save_config(secrets_dir=path)

    # ── Reaktionen ─────────────────────────────────────────────────────────────
    def _on_neu_changed(self):
        """Jahr/Area/Ausgabename aus dem Namen von DSM aktuell, dann die Datei-Info.
        Der Name gewinnt gegen einen alten Eintrag (wie im DMC-Converter: ein Handeintrag
        kann von einem frueheren Gebiet stehengeblieben sein). Ein von Hand gewaehlter
        Ausgabepfad bleibt."""
        path = self.neu_var.get().strip().strip('"')
        if path == getattr(self, "_last_neu_path", None):
            return                          # nur Fokuswechsel, nichts geaendert
        self._last_neu_path = path
        self._neu_gsd_m = None
        self._s3d_result = None
        if stac_import.is_url(path):
            # Vorbelegung kommt mit der Info aus den STAC-Metadaten (_apply_stac_neu). Ein
            # Ordner fuer die Ausgabe laesst sich aus einem Link nicht ableiten.
            self._refresh_info(self.neu_var, self.neu_info_lbl, self.neu_problem_lbl,
                               on_stac=self._apply_stac_neu)
            return
        parsed = parse_dsm_name(path)
        if parsed:
            self.jahr_var.set(parsed["jahr"])
            self.area_var.set(parsed["area"])
            self._neu_gsd_m = parsed["gsd_cm"] / 100.0
        out = self.out_var.get().strip()
        if path and (not out or out == self._out_auto):
            folder = os.path.dirname(path)
            name = (diff_filename(parsed["jahr"], parsed["area"], parsed["gsd_cm"]) if parsed
                    else f"{Path(path).stem}_DSM-DIFF.tif")
            self._out_auto = os.path.join(folder, name)
            self.out_var.set(self._out_auto)
        self._refresh_info(self.neu_var, self.neu_info_lbl, self.neu_problem_lbl)

    def _refresh_alt_info(self):
        self._refresh_info(self.alt_var, self.alt_info_lbl, self.alt_problem_lbl,
                           on_stac=self._apply_stac_alt)

    def _apply_stac_neu(self, st):
        """Felder aus den STAC-Metadaten von DSM aktuell vorbelegen (alles editierbar)."""
        filled = []
        code = st.get("auftragstyp")
        if code:
            self.auftragstyp_var.set(next(l for l in AUFTRAGSTYP_LABELS
                                          if l.split(" - ", 1)[0] == code))
            filled.append(f"Auftragstyp {code}")
        if st.get("area"):
            self.area_var.set(st["area"])
            filled.append(f"Area {st['area']}")
        if st.get("line_ids"):
            # Liste zuerst leeren: sonst entfernt der Kamerawechsel alte IDs mit Rueckfrage
            self.lineid_w.clear()
        if st.get("camera") in stac_import.CAMERA_SYSTEMS:
            self.camera_var.set(st["camera"])
            filled.append(f"CameraSystem {st['camera']}")
        if st.get("line_ids"):
            added, skipped = self.lineid_w.add_ids(st["line_ids"])
            filled.append(f"{len(added)} LineID(s)")
            if skipped:
                self._log("STAC-Vorbelegung, nicht übernommene LineIDs:\n" + "\n".join(skipped) + "\n")
        if st.get("year"):
            self.jahr_var.set(str(st["year"]))
            filled.append(f"Jahr {st['year']}")
        if st.get("gsd"):
            self._neu_gsd_m = float(st["gsd"])
        self._preview_sig = None
        if filled:
            self._log(f"Vorbelegt aus STAC ({st.get('asset')}): {', '.join(filled)}\n")

    def _apply_stac_alt(self, st):
        """TerrainModel 2 und dessen Jahr aus den STAC-Metadaten von DSM alt."""
        if st.get("terrain_model"):
            self.tm2_var.set(st["terrain_model"])
        if st.get("year"):
            self.tm2_year_var.set(str(st["year"]))
        self._preview_sig = None

    def _on_alt_change(self):
        s3d = self.alt_mode_var.get() == ALT_S3D
        self._set_enabled(self._s3d_frame, s3d)
        self._set_enabled(self._file_frame, not s3d)
        if s3d:
            self._tm2_file_frame.pack_forget()
            self.tm2_s3d_lbl.pack(side="left")
        else:
            self.tm2_s3d_lbl.pack_forget()
            self._tm2_file_frame.pack(side="left")

    def _on_camera_change(self):
        removed = self.lineid_w.refresh_camera()
        if removed:
            messagebox.showinfo("LineIDs entfernt",
                                "Diese LineIDs passen nicht zum Format von "
                                f"{self.camera_var.get()} und wurden entfernt:\n\n"
                                + "\n".join(removed), parent=self)

    @staticmethod
    def _set_enabled(frame, enabled):
        for w in frame.winfo_children():
            try:
                w.state(["!disabled"] if enabled else ["disabled"])
            except (AttributeError, tk.TclError):
                try:
                    w.configure(state="normal" if enabled else "disabled")
                except tk.TclError:
                    pass
            if w.winfo_children():
                DSMDiffApp._set_enabled(w, enabled)

    def _auftragstyp(self):
        return self.auftragstyp_var.get().split(" - ", 1)[0].strip()

    def _acq_year(self):
        return acquisition_year(self.lineid_w.get_ids(), self.camera_var.get(), self.jahr_var.get())

    def _s3d_key_now(self):
        year, _ = self._acq_year()
        return (self.neu_var.get().strip().strip('"'), year)

    def _tm2_preview(self):
        if self.alt_mode_var.get() == ALT_S3D:
            if self._s3d_result and self._s3d_key == self._s3d_key_now():
                return stac_import.terrain_model_2(stac_import.TERRAIN_MODEL_S3D,
                                                   self._s3d_result.get("years"))
            return f"{stac_import.TERRAIN_MODEL_S3D} (<Jahre nach 'Kacheln suchen' bzw. aus dem Lauf>)"
        return stac_import.terrain_model_2(self.tm2_var.get(), [self.tm2_year_var.get()])

    # ── Vorschau (laufend ueber den Poll-Zyklus, deckt auch die LineID-Liste ab) ─
    def _preview_values(self):
        ids = self.lineid_w.get_ids()
        cam = self.camera_var.get()
        env = self.env_var.get()
        dt = item = asset = link = "–"
        try:
            if ids:
                item = stac_import.item_id(self._auftragstyp(), ids, cam)
                dt = item.split("-", 1)[1]
                asset = stac_import.asset_name(item)
                link = stac_import.browser_link(item, env)
        except ValueError:
            pass
        try:
            desc = stac_import.build_description(
                self.area_var.get().strip() or "<AREA>",
                [self.tm1_var.get(), self._tm2_preview()], cam, ids,
                self.commentary_var.get().strip() or stac_import.DEFAULT_COMMENTARY)
        except ValueError as e:
            desc = f"({e})"
        return {"dt": dt, "item": item, "asset": asset, "title": stac_import.ASSET_TITLE,
                "link": link, "desc": desc}

    def _refresh_preview(self):
        sig = (tuple(self.lineid_w.get_ids()), self.camera_var.get(), self.env_var.get(),
               self.auftragstyp_var.get(), self.area_var.get(), self.jahr_var.get(),
               self.tm1_var.get(), self.tm2_var.get(), self.tm2_year_var.get(),
               self.commentary_var.get(), self.alt_mode_var.get(),
               self.out_var.get(), self.neu_var.get(), id(self._s3d_result))
        if sig == self._preview_sig:
            return
        self._preview_sig = sig
        for key, val in self._preview_values().items():
            self._preview[key].configure(text=val)
        year, source = self._acq_year()
        self.s3d_rule_lbl.configure(
            text=(f"jüngstes swissSURFACE3D-Jahr vor {year}  (Aufnahmejahr aus {source}; "
                  f"das Jahr je Kachel steht im STAC-Item)"
                  if year else "Aufnahmejahr fehlt – LineIDs erfassen oder Feld Jahr ausfüllen"))
        if self.alt_mode_var.get() == ALT_S3D:
            self.mosaic_lbl.configure(text=mosaic_path_for(self.out_var.get(), self.jahr_var.get().strip(),
                                                           self.area_var.get().strip()) or "–")
        else:
            self.mosaic_lbl.configure(text="– (nur bei swissSURFACE3D)")
        if self._s3d_result is not None and self._s3d_key != self._s3d_key_now():
            self._s3d_result = None
            self.s3d_result_lbl.configure(text="(Eingaben geändert – neu suchen oder Lauf starten)")

    def _open_link(self):
        link = self._preview["link"].cget("text")
        if link.startswith("http"):
            try:
                webbrowser.open(link)
            except Exception:
                pass

    # ── Runner-Aufrufe mit JSON-Antwort (info, s3d_search) ─────────────────────
    def _set_busy(self, busy):
        """Sanduhr ('watch', in Tk eingebaut) solange ein Hintergrund-Aufruf laeuft (Info,
        Kacheln suchen). Zaehler, weil zwei Aufrufe gleichzeitig laufen koennen."""
        self._busy_count = max(0, getattr(self, "_busy_count", 0) + (1 if busy else -1))
        try:
            self.configure(cursor="watch" if self._busy_count else "")
        except tk.TclError:
            pass

    def _fetch_json_async(self, cfg, on_ok, on_err, log_stderr=False):
        self._set_busy(True)
        done_ok, done_err = on_ok, on_err

        def on_ok(result):
            self._set_busy(False)
            done_ok(result)

        def on_err(msg):
            self._set_busy(False)
            done_err(msg)

        def worker():
            tmp_name = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                                 encoding="utf-8") as tmp:
                    json.dump(cfg, tmp, ensure_ascii=False)
                    tmp_name = tmp.name
                result = subprocess.run([self._osgeo_python, RUNNER_SCRIPT, tmp_name],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        universal_newlines=True, encoding="utf-8",
                                        errors="replace", env=_runner_env(self._osgeo_python))
                if log_stderr and result.stderr:
                    self._log_q.put(("log", result.stderr))
                if result.returncode != 0:
                    err = [l for l in (result.stderr or "").splitlines() if l.startswith("FEHLER:")]
                    msg = "\n".join(err) or (result.stderr or result.stdout or "").strip()[-800:]
                    self.after(0, on_err, msg)
                    return
                lines = [l for l in (result.stdout or "").splitlines() if l.strip()]
                self.after(0, on_ok, json.loads(lines[-1] if lines else "{}"))
            except Exception as e:
                self.after(0, on_err, str(e))
            finally:
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_info(self, var, info_lbl, problem_lbl, force=False, on_stac=None, on_diff=None):
        """Info-Zeile ueber die Runner-Aktion 'info'. STAC-Link: Metadaten statt Datei-Info,
        on_stac(stac_info) belegt danach die Felder vor; on_diff(dsmdiff_metadaten) ebenso
        fuer ein vorhandenes DIFF."""
        path = var.get().strip().strip('"')
        # Fokuswechsel ohne neuen Pfad startet keinen neuen Subprocess
        if not force and getattr(info_lbl, "_checked_path", None) == path:
            return
        info_lbl._checked_path = path
        problem_lbl.configure(text="")
        if not path:
            info_lbl.configure(text="–")
            return
        is_link = stac_import.is_url(path)
        if is_link:
            try:
                stac_import.parse_asset_href(path)
            except ValueError as e:
                info_lbl.configure(text="–")
                problem_lbl.configure(text="⚠ " + str(e).splitlines()[0])
                return
        elif not os.path.isfile(path):
            info_lbl.configure(text="–")
            problem_lbl.configure(text="⚠ Datei nicht gefunden")
            return
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            info_lbl.configure(text="OSGeo4W Python nicht gefunden – bitte Pfad setzen")
            return
        info_lbl.configure(text="⏳ STAC-Metadaten werden gelesen…" if is_link else "⏳ wird gelesen…")

        def ok(info):
            if var.get().strip().strip('"') != path:
                return                      # inzwischen eine andere Datei gewaehlt
            info_lbl.configure(text=self._format_info(info))
            if info.get("problem"):
                problem_lbl.configure(text="⚠ " + info["problem"])
            elif info.get("stac") and on_stac:
                on_stac(info["stac"])
            elif on_diff:
                on_diff(info.get("dsmdiff") or {})

        def err(msg):
            info_lbl.configure(text="Datei-Info nicht lesbar (Details im Log)")
            self._log(f"Datei-Info {os.path.basename(path)}: {msg}\n")

        self._fetch_json_async({"action": "info", "input_path": path,
                                "secrets_dir": self.secrets_var.get().strip()}, ok, err,
                               log_stderr=is_link)

    @staticmethod
    def _format_info(info):
        parts = []
        st = info.get("stac")
        if st:
            parts.append(f"STAC-Asset {st.get('asset')} ({st.get('env')})")
        diff_item = (info.get("dsmdiff") or {}).get("DSMDIFF_STAC_ITEM")
        if diff_item:
            parts.append(f"DIFF für Item {diff_item}")
        if info.get("res_x"):
            parts.append(f"GSD {info['res_x']:g} m")
        if info.get("width"):
            parts.append(f"{info['width']} × {info['height']} px")
        parts.append(info.get("srs_name") or "kein CRS")
        tag, name = info.get("vertical_tag"), info.get("vertical_name")
        stac_ref = info.get("vertical_stac")
        labels = {5728: "LN02", 5729: "LHN95"}
        if tag:
            parts.append(f"Höhenbezug {labels.get(tag, tag)} (Tag)")
        elif stac_ref:
            parts.append(f"Höhenbezug {labels.get(stac_ref, stac_ref)} (STAC-Beschreibung)")
        elif name:
            parts.append(f"Höhenbezug {labels.get(name, name)} (nur Dateiname)")
        elif "vertical_tag" in info or st:
            parts.append("Höhenbezug nicht getaggt")
        if not st:
            nd = info.get("nodata")
            parts.append("kein NoData-Tag" if nd is None else f"NoData {nd:g}")
        if info.get("dtype"):
            parts.append(info["dtype"])
        if info.get("size_mb") is not None:
            parts.append(f"{info['size_mb']:.1f} MB")
        return "  |  ".join(parts)

    def _search_tiles(self):
        errors = []
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append("OSGeo4W Python nicht gefunden.")
        neu = self.neu_var.get().strip().strip('"')
        is_link = stac_import.is_url(neu)
        if not neu or (not is_link and not os.path.isfile(neu)):
            errors.append("DSM aktuell fehlt oder ist nicht vorhanden.")
        year, _ = self._acq_year()
        if not year:
            errors.append("Aufnahmejahr fehlt – LineIDs erfassen oder Feld Jahr ausfüllen.")
        if not self._show_errors(errors):
            return
        key = self._s3d_key_now()
        self.s3d_search_btn.state(["disabled"])
        self.s3d_result_lbl.configure(
            text="⏳ Ausdehnung aus dem Dateikopf des Links (ca. 1 MB), dann Suche im STAC …"
                 if is_link else "⏳ suche im STAC …")

        def ok(res):
            self.s3d_search_btn.state(["!disabled"])
            self._s3d_result, self._s3d_key = res, key
            self._preview_sig = None
            self.s3d_result_lbl.configure(text=self._format_s3d(res))

        def err(msg):
            self.s3d_search_btn.state(["!disabled"])
            self._s3d_result = None
            self.s3d_result_lbl.configure(text="Suche fehlgeschlagen (Details im Log)")
            self._log(f"Kacheln suchen: {msg}\n")

        self._fetch_json_async({"action": "s3d_search", "dsm_neu": neu, "ref_year": year,
                                "secrets_dir": self.secrets_var.get().strip(),
                                "staging_dir": self._staging_dir},
                               ok, err, log_stderr=True)

    @staticmethod
    def _format_s3d(res):
        chosen = res.get("chosen", {})
        counts = {}
        for y in chosen.values():
            counts[y] = counts.get(y, 0) + 1
        lines = [f"{len(res.get('needed', []))} Kachel(n) im Perimeter  |  gewählt (Jahr vor "
                 f"{res.get('ref_year')}): "
                 + (", ".join(f"{y} ({n}×)" for y, n in sorted(counts.items())) or "keine")]
        if res.get("missing"):
            lines.append(f"fehlend (→ NoData): {', '.join(res['missing'])}")
        if res.get("no_suitable_year"):
            lines.append("ohne passendes Jahr (→ NoData): " + ", ".join(
                f"{k} (nur {', '.join(map(str, v))})" for k, v in res["no_suitable_year"].items()))
        if res.get("mixed_years"):
            lines.append("⚠ GEMISCHTE JAHRE – die Referenz ist nicht einheitlich")
        return "\n".join(lines)

    # ── Validierung (nur Formales) ─────────────────────────────────────────────
    def _validate(self, import_only=False):
        errors = []
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append("OSGeo4W Python nicht gefunden.\nBitte Pfad via 'Ändern…' festlegen "
                          "(z.B. C:\\Program Files\\QGIS 3.44\\bin\\python3.exe).")
        out = normalize_tif_path(self.out_var.get())
        stac_on = import_only or self.stac_var.get()

        if import_only:
            diff = normalize_tif_path(self.import_diff_var.get())
            if not diff or not os.path.isfile(diff):
                errors.append(f"Vorhandenes DIFF-COG (Abschnitt STAC-Import) nicht gefunden:\n  "
                              f"{diff or '(leer)'}")
        else:
            neu = self.neu_var.get().strip().strip('"')
            alt = self.alt_var.get().strip().strip('"')
            self._validate_input(errors, "DSM aktuell", neu)
            if self.alt_mode_var.get() == ALT_FILE:
                self._validate_input(errors, "DSM alt (eigenes DSM)", alt)
                if stac_on and not self.tm2_var.get().strip():
                    errors.append("TerrainModel 2 fehlt.")
            elif not self._acq_year()[0]:
                errors.append("Aufnahmejahr fehlt (LineIDs oder Feld Jahr) – es bestimmt das "
                              "swissSURFACE3D-Jahr je Kachel.")
            if not out:
                errors.append("DIFF-COG (Ausgabe) fehlt.")
            elif not os.path.isabs(out) or not out.lower().endswith(".tif"):
                errors.append("DIFF-COG: vollständiger Pfad mit Endung .tif erwartet.")
            elif not os.path.isdir(os.path.dirname(out)):
                errors.append(f"Ausgabeordner existiert nicht:\n  {os.path.dirname(out)}")
            else:
                same = {os.path.normcase(os.path.abspath(p)) for p in (neu, alt)
                        if p and not stac_import.is_url(p)}
                if os.path.normcase(os.path.abspath(out)) in same:
                    errors.append("DIFF-COG darf keine Eingabe überschreiben.")
                mos = mosaic_path_for(out, self.jahr_var.get().strip(), self.area_var.get().strip())
                if self.alt_mode_var.get() == ALT_S3D and \
                        os.path.normcase(os.path.abspath(mos)) in same:
                    errors.append("Das swissSURFACE3D-Mosaik darf keine Eingabe überschreiben.")
            if not self._staging_dir:
                errors.append("Staging-Ordner fehlt.")

        if stac_on:
            if not self.lineid_w.get_ids():
                errors.append("STAC-Import: LineIDs fehlen (daraus kommt die Item-ID).")
            if not self.area_var.get().strip():
                errors.append("STAC-Import: Area fehlt.")
            if self.env_var.get() not in stac_import.STAC_HOSTS:
                errors.append("STAC-Import: Umgebung INT oder PROD wählen.")
            sec = self.secrets_var.get().strip()
            if not sec or not os.path.isdir(sec):
                errors.append(f"Secrets-Ordner nicht gefunden:\n  {sec or '(leer)'}")
        return errors

    @staticmethod
    def _validate_input(errors, role, path):
        """Lokale Datei (.tif vorhanden) oder STAC-Link (Form: Host INT/PROD, .tif-Asset)."""
        if not path:
            errors.append(f"{role} fehlt.")
        elif stac_import.is_url(path):
            try:
                stac_import.parse_asset_href(path)
            except ValueError as e:
                errors.append(f"{role}: {e}")
        elif not os.path.isfile(path):
            errors.append(f"{role} nicht gefunden:\n  {path}")
        elif not path.lower().endswith((".tif", ".tiff")):
            errors.append(f"{role}: GeoTIFF/COG (.tif) erwartet.")

    def _show_errors(self, errors):
        if not errors:
            return True
        messagebox.showerror("Eingabe-Fehler", "\n\n".join(f"• {e}" for e in errors), parent=self)
        return False

    def _confirm_prod(self, out):
        if self.env_var.get() != "PROD":
            return True
        item = stac_import.item_id(self._auftragstyp(), self.lineid_w.get_ids(), self.camera_var.get())
        return ProdConfirmDialog(self, item, stac_import.asset_name(item), out,
                                 dark=self._dark).wait()

    # ── Start ──────────────────────────────────────────────────────────────────
    def _meta_cfg(self):
        return {"auftragstyp": self._auftragstyp(), "camera": self.camera_var.get(),
                "line_ids": self.lineid_w.get_ids(), "area": self.area_var.get().strip(),
                "tm1": self.tm1_var.get(), "tm2": self.tm2_var.get(),
                "tm2_year": self.tm2_year_var.get().strip(),
                "commentary": self.commentary_var.get().strip() or stac_import.DEFAULT_COMMENTARY}

    def _stac_cfg(self, enabled):
        return {"enabled": bool(enabled), "env": self.env_var.get(),
                "secrets_dir": self.secrets_var.get().strip(),
                "replace_existing": bool(self.replace_var.get())}

    def _build_cfg(self):
        out = normalize_tif_path(self.out_var.get())
        s3d = self.alt_mode_var.get() == ALT_S3D
        return {
            "action": "dsm_diff",
            "dsm_neu": self.neu_var.get().strip().strip('"'),
            "out_path": out,
            "alt_quelle": self.alt_mode_var.get(),
            "dsm_alt": None if s3d else self.alt_var.get().strip().strip('"'),
            "ref_year": self._acq_year()[0] if s3d else None,
            "mosaic_out": mosaic_path_for(out, self.jahr_var.get().strip(),
                                          self.area_var.get().strip()) if s3d else None,
            # gleichnamige Ausgaben immer ersetzen (Wunsch Nutzer); sicher, weil erst nach der
            # Pruefung per os.replace getauscht wird
            "overwrite": True,
            "staging_dir": self._staging_dir,
            "keep_staging": bool(self.keep_staging_var.get()),
            "block_size": 2048,
            "meta": self._meta_cfg(),
            "stac": self._stac_cfg(self.stac_var.get()),
        }

    def _start(self):
        if self._running or not self._show_errors(self._validate()):
            return
        out = normalize_tif_path(self.out_var.get())
        self.out_var.set(out)
        if self.stac_var.get() and not self._confirm_prod(out):
            return
        cfg = self._build_cfg()
        self._pending_link = (self._preview["link"].cget("text") if self.stac_var.get() else None)
        self._last_diff_out = out
        self._launch(cfg, "DSM-DIFF", f"DSMDIFF_{Path(out).stem}")

    def _start_import_only(self):
        if self._running or not self._show_errors(self._validate(import_only=True)):
            return
        diff = normalize_tif_path(self.import_diff_var.get())
        if not self._confirm_prod(diff):
            return
        cfg = {"action": "stac_import", "diff_path": diff, "meta": self._meta_cfg(),
               "stac": self._stac_cfg(True)}
        self._pending_link = self._preview["link"].cget("text")
        self._last_diff_out = None
        self._launch(cfg, "STAC-Import", f"STACIMPORT_{Path(diff).stem}")

    def _launch(self, cfg, vorgang, log_stem):
        self._running = True
        self._active_vorgang = vorgang
        self._cancel_file = os.path.join(
            tempfile.gettempdir(),
            f"dsmdiff_cancel_{os.getpid()}_{datetime.datetime.now():%Y%m%d_%H%M%S}.flag")
        try:
            if os.path.exists(self._cancel_file):
                os.remove(self._cancel_file)
        except OSError:
            pass
        cfg["cancel_file"] = self._cancel_file
        T = DARK if self._dark else LIGHT
        self.start_btn.config(state="disabled", fg=T["fg_dim"], disabledforeground=T["fg_dim"])
        self.import_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self._progress_start = None
        self._phase, self._phase_group, self._last_frac = "", None, 0.0
        self._progress_bar.config(mode="indeterminate")
        self._progress_lbl.config(text="Verarbeitung läuft…")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        if not self._log_visible:
            self._toggle_log()
        self._clear_log()
        self._error_lines = []
        self._log(f"=== {vorgang} gestartet ===\n\n")
        threading.Thread(target=self._run_thread, args=(cfg, log_stem, vorgang),
                         daemon=True).start()

    def _run_thread(self, cfg, log_stem, vorgang):
        code = EXIT_ERROR
        try:
            code = self._run_osgeo_subprocess(cfg, log_stem)
        except Exception as e:
            self._log_q.put(("log", f"\nFEHLER: {e}\n{traceback.format_exc()}"))
        finally:
            self._log_q.put(("done", (code, vorgang)))

    def _run_osgeo_subprocess(self, cfg, log_stem):
        """Startet den Runner; Log + Fortschritt live ins GUI, Logdatei in logs/."""
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"{log_stem}_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.log")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp_name = tmp.name
        try:
            header = f"[Subprocess] {self._osgeo_python}\n\n"
            self._log_q.put(("log", header))
            proc = subprocess.Popen([self._osgeo_python, RUNNER_SCRIPT, tmp_name],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True, encoding="utf-8", errors="replace",
                                    env=_runner_env(self._osgeo_python))
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write(header)
                for line in proc.stdout:
                    stripped = line.strip()
                    if stripped.startswith("PROGRESS:"):
                        try:
                            self._log_q.put(("progress", float(stripped.split(":", 1)[1])))
                        except ValueError:
                            pass
                        continue    # Steuerzeile - nicht ins Log
                    if stripped.startswith("PHASE:"):
                        self._log_q.put(("phase", stripped.split(":", 1)[1].strip()))
                        continue    # Steuerzeile - nicht ins Log
                    self._log_q.put(("log", line))
                    lf.write(line)
            proc.wait()
            self._log_q.put(("log", f"\nLog gespeichert: {log_path}\n"))
            return proc.returncode
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _cancel(self):
        if not self._running or not self._cancel_file:
            return
        try:
            with open(self._cancel_file, "w", encoding="utf-8") as f:
                f.write("abbrechen\n")
        except OSError as e:
            self._log(f"\nFEHLER: Abbruch-Markierung nicht schreibbar: {e}\n")
            return
        self.cancel_btn.state(["disabled"])
        self._log("\n>>> Abbruch angefordert – der Lauf hält beim nächsten Block/Teil an "
                  "und räumt auf.\n")

    def _on_done(self, code, vorgang):
        self._running = False
        self._progress_bar.stop()
        self._progress_frame.pack_forget()
        self.cancel_btn.state(["disabled"])
        self.import_btn.state(["!disabled"])
        try:
            if self._cancel_file and os.path.exists(self._cancel_file):
                os.remove(self._cancel_file)
        except OSError:
            pass
        self._update_start_btn_state()
        if self._close_after_run:
            self.destroy()
            return
        detail = "\n".join(dict.fromkeys(self._error_lines))[:1500] or "Details siehe Log."
        # Das frisch gerechnete DIFF steht sofort fuer 'Nur STAC-Import' bereit
        new_diff = getattr(self, "_last_diff_out", None)
        if code in (EXIT_OK, EXIT_IMPORT_FAILED) and new_diff and os.path.isfile(new_diff):
            self.import_diff_var.set(new_diff)
            self.import_diff_info_lbl._checked_path = None
        if code == EXIT_OK:
            self._log(f"\n✓  {vorgang} erfolgreich abgeschlossen.\n")
            link = self._pending_link if self._pending_link and \
                self._pending_link.startswith("http") else None
            if link and messagebox.askyesno(f"{vorgang} abgeschlossen",
                                            f"{vorgang} erfolgreich abgeschlossen.\n\n"
                                            f"STAC-Item im Browser öffnen?\n{link}", parent=self):
                webbrowser.open(link)
            elif not link:
                messagebox.showinfo(f"{vorgang} abgeschlossen",
                                    f"{vorgang} erfolgreich abgeschlossen.", parent=self)
        elif code == EXIT_CANCEL:
            self._log(f"\n✗  {vorgang} abgebrochen – nichts abgelegt.\n")
            messagebox.showinfo("Abgebrochen", f"{vorgang} wurde abgebrochen.\n"
                                               "Es wurde nichts abgelegt, das Staging ist aufgeräumt.",
                                parent=self)
        elif code == EXIT_IMPORT_FAILED:
            self._log("\n⚠  DIFF erstellt, STAC-Import fehlgeschlagen.\n")
            messagebox.showwarning("STAC-Import fehlgeschlagen",
                                   "Das DIFF ist fertig und geprüft, der STAC-Import ist aber "
                                   f"fehlgeschlagen:\n\n{detail}\n\nDas DIFF steht unter "
                                   "STAC-Import → 'Vorhandenes DIFF-COG' bereit; Import dort mit "
                                   "'Nur STAC-Import dieses DIFF' nachholen.", parent=self)
        else:
            self._log(f"\n✗  {vorgang} fehlgeschlagen.\n")
            messagebox.showerror(f"{vorgang} fehlgeschlagen",
                                 f"{vorgang} ist fehlgeschlagen:\n\n{detail}", parent=self)

    def _on_close(self):
        if self._running:
            if messagebox.askyesno("Lauf aktiv", "Ein Lauf ist aktiv. Abbrechen und das Fenster "
                                                 "schliessen, sobald aufgeräumt ist?", parent=self):
                self._close_after_run = True
                self._cancel()
            return
        self.destroy()

    # ── Poll-Zyklus: Log, Fortschritt, Vorschau, Start-Button ─────────────────
    def _poll(self):
        while True:
            try:
                kind, data = self._log_q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(data)
            elif kind == "progress":
                self._update_progress(data)
            elif kind == "phase":
                self._set_phase(data)
            elif kind == "done":
                self._on_done(*data)
        try:
            self._refresh_preview()
            self._update_start_btn_state()
        except tk.TclError:
            return
        self.after(120, self._poll)

    def _update_start_btn_state(self):
        """Start erst aktiv, wenn die noetigen Felder einen Eintrag haben (amber/gruen
        wie im GDWH-Import). Die eigentliche Pruefung macht _validate beim Klick."""
        if self._running or not hasattr(self, "start_btn"):
            return
        T = DARK if self._dark else LIGHT
        ok = (bool(self._osgeo_python) and bool(self.neu_var.get().strip())
              and bool(self.out_var.get().strip()) and bool(self._staging_dir))
        if self.alt_mode_var.get() == ALT_FILE:
            ok = ok and bool(self.alt_var.get().strip())
        else:
            ok = ok and self._acq_year()[0] is not None
        if self.stac_var.get():
            ok = ok and bool(self.lineid_w.get_ids()) and bool(self.area_var.get().strip())
        color = T["ok"] if ok else T["hint"]
        self.start_btn.config(state="normal" if ok else "disabled", fg=color,
                              disabledforeground=color, bg=T["btn"],
                              activebackground=T["btn_hover"])

    def _update_progress(self, fraction):
        if self._progress_start is None:
            self._progress_start = time.time()
            self._eta_t0, self._eta_f0 = self._progress_start, 0.0
            self._progress_bar.stop()
            self._progress_bar.config(mode="determinate", maximum=100)
        self._last_frac = max(0.0, min(1.0, fraction))
        self._progress_bar["value"] = self._last_frac * 100.0
        self._render_progress()

    def _set_phase(self, text):
        """Laufender Schritt vom Runner ('PHASE:'-Zeile). Wechselt die Art des Schritts
        (Download / Upload / Rechnung), beginnt die Restzeit-Schaetzung neu - ein Download
        laeuft in einem ganz anderen Tempo als die Rechnung danach."""
        group = text.split(":")[0].split(" ")[0]
        if group != getattr(self, "_phase_group", None):
            self._phase_group = group
            self._eta_t0, self._eta_f0 = time.time(), getattr(self, "_last_frac", 0.0)
        self._phase = text
        if self._progress_start is not None:
            self._render_progress()

    def _render_progress(self):
        pct = getattr(self, "_last_frac", 0.0)
        phase = getattr(self, "_phase", "")
        parts = [f"{pct * 100:5.1f}%"]
        if phase:
            parts.append(phase)
        # Bei Download/Upload zeigt die Phase selbst den Stand (MB, Teile) - eine Restzeit
        # aus dem Tempo der Uebertragung waere fuer die Rechnung danach irrefuehrend
        if not phase.startswith(("Download", "Upload")):
            done = pct - getattr(self, "_eta_f0", 0.0)
            eta = "--:--"
            if done > 0.005:
                rest = (time.time() - self._eta_t0) * (1.0 - pct) / done
                eta = f"{int(rest // 60):d}m {int(rest % 60):02d}s"
            parts.append(f"verbleibend: {eta}")
        self._progress_lbl.config(text="  —  ".join(parts))

    # ── Log ────────────────────────────────────────────────────────────────────
    def _log(self, text):
        for line in text.splitlines():
            if line.strip().startswith("FEHLER:"):
                self._error_lines.append(line.strip())
        self.log_box.config(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _toggle_log(self):
        if self._log_visible:
            self._log_frame.pack_forget()
            self._log_sep.pack_forget()
            self._terminal_btn.config(text="Terminal ▾")
            self._log_visible = False
        else:
            self._log_sep.pack(fill="x", padx=12, pady=4, before=self._btn_row)
            self._log_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
            self._terminal_btn.config(text="Terminal ▴")
            self._log_visible = True

    # ── Umgebung ───────────────────────────────────────────────────────────────
    def _update_osgeo_label(self):
        T = DARK if self._dark else LIGHT
        if self._osgeo_python and os.path.isfile(self._osgeo_python):
            self._osgeo_lbl.config(text=self._osgeo_python)
            self._osgeo_status.config(text="✓", foreground=T["ok"])
        else:
            self._osgeo_lbl.config(text=self._osgeo_python or "(nicht gefunden)")
            self._osgeo_status.config(text="✗ nicht gefunden", foreground=T["err"])

    def _set_osgeo_python(self):
        init_dir = os.path.dirname(self._osgeo_python) if self._osgeo_python else "C:\\"
        if not os.path.isdir(init_dir):
            init_dir = "C:\\"
        path = filedialog.askopenfilename(title="OSGeo4W Python auswählen", initialdir=init_dir,
                                          filetypes=[("Python", "python*.exe"),
                                                     ("Executable", "*.exe"), ("Alle", "*.*")])
        if path:
            self._osgeo_python = path.replace("/", "\\")
            _save_config(osgeo_python=self._osgeo_python)
            self._update_osgeo_label()

    def _update_staging_label(self):
        T = DARK if self._dark else LIGHT
        self._staging_lbl.config(text=self._staging_dir or "(nicht gesetzt)")
        drive = os.path.splitdrive(self._staging_dir)[0]
        if self._staging_dir and (not drive or os.path.isdir(drive + "\\")):
            self._staging_status.config(text="✓", foreground=T["ok"])
        else:
            self._staging_status.config(text="✗ Laufwerk nicht verfügbar", foreground=T["err"])

    def _set_staging_dir(self):
        init_dir = self._staging_dir if os.path.isdir(self._staging_dir or "") else "C:\\"
        path = filedialog.askdirectory(title="Staging-Ordner wählen (z.B. Y:\\ oder D:\\)",
                                       initialdir=init_dir)
        if path:
            self._staging_dir = path.replace("/", "\\")
            _save_config(staging_dir=self._staging_dir)
            self._update_staging_label()

    # ── Theme (aus topo-GDWHimport) ────────────────────────────────────────────
    def _toggle_theme(self):
        self._apply_theme(not self._dark)

    def _apply_theme(self, dark):
        self._dark = dark
        T = DARK if dark else LIGHT
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=T["panel"], foreground=T["fg"], fieldbackground=T["input"],
                    selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
                    bordercolor=T["sep"], lightcolor=T["panel"], darkcolor=T["sep"],
                    insertcolor=T["fg"], troughcolor=T["root"])
        s.configure("TFrame", background=T["panel"])
        s.configure("TLabelframe", background=T["panel"], bordercolor=T["sep"])
        s.configure("TLabelframe.Label", background=T["panel"], foreground=T["fg"],
                    font=("Segoe UI", 9, "bold"))
        s.configure("Section.TLabelframe", background=T["panel"], bordercolor=T["sep"])
        s.configure("Section.TLabelframe.Label", background=T["panel"], foreground=T["accent"],
                    font=("Segoe UI", 10, "bold"))
        s.configure("TLabel", background=T["panel"], foreground=T["fg"])
        # clam zeichnet DEAKTIVIERTE Elemente (hier: die gerade nicht gewaehlte Quelle DSM alt)
        # mit hellem Grund - im Darkmode weisse Felder. Darum auch 'disabled' einfaerben.
        s.map(".", background=[("disabled", T["panel"])],
              foreground=[("disabled", T["fg_dim"])])
        s.map("TLabel", background=[("disabled", T["panel"])],
              foreground=[("disabled", T["fg_dim"])])
        s.configure("TButton", background=T["btn"], foreground=T["fg"], bordercolor=T["sep"],
                    relief="flat", padding=(8, 4), focuscolor=T["panel"])
        s.map("TButton", background=[("active", T["btn_hover"]), ("pressed", T["sep"])],
              foreground=[("active", T["fg"]), ("disabled", T["fg_dim"])],
              relief=[("pressed", "flat")])
        s.configure("Small.TButton", padding=(6, 0), font=("", 8))
        # Indikator (Kreis/Kaestchen) sonst immer weiss, auch im Darkmode
        for style in ("TRadiobutton", "TCheckbutton"):
            s.configure(style, background=T["panel"], foreground=T["fg"], focuscolor=T["panel"],
                        indicatorbackground=T["input"], indicatorforeground=T["accent"],
                        upperbordercolor=T["sep"], lowerbordercolor=T["sep"])
            s.map(style, background=[("disabled", T["panel"]), ("active", T["panel"])],
                  foreground=[("disabled", T["fg_dim"]), ("active", T["fg"])],
                  indicatorbackground=[("disabled", T["panel"]), ("pressed", T["btn_hover"]),
                                       ("selected", T["input"]), ("active", T["input"])],
                  indicatorforeground=[("disabled", T["fg_dim"])])
        s.configure("TCombobox", fieldbackground=T["input"], background=T["btn"],
                    foreground=T["fg"], arrowcolor=T["fg"], selectbackground=T["sel_bg"],
                    selectforeground=T["sel_fg"], bordercolor=T["sep"], insertcolor=T["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", T["input"]), ("disabled", T["panel"])],
              selectbackground=[("readonly", T["input"])],
              selectforeground=[("readonly", T["fg"])],
              foreground=[("disabled", T["fg_dim"]), ("readonly", T["fg"])],
              background=[("active", T["btn_hover"])])
        s.configure("TEntry", fieldbackground=T["input"], foreground=T["fg"], bordercolor=T["sep"],
                    insertcolor=T["fg"], selectbackground=T["sel_bg"], selectforeground=T["sel_fg"])
        s.map("TEntry", fieldbackground=[("disabled", T["panel"])],
              foreground=[("disabled", T["fg_dim"])])
        s.configure("Vertical.TScrollbar", background=T["scroll"], troughcolor=T["root"],
                    bordercolor=T["sep"], arrowcolor=T["fg"])
        s.map("Vertical.TScrollbar", background=[("active", T["scroll"]), ("pressed", T["scroll"])])
        s.configure("TSeparator", background=T["sep"])
        s.configure("TProgressbar", background=T["accent"], troughcolor=T["root"],
                    bordercolor=T["sep"])

        self.option_add("*TCombobox*Listbox.background", T["list"])
        self.option_add("*TCombobox*Listbox.foreground", T["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", T["sel_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", T["sel_fg"])

        self.configure(bg=T["root"])
        self._canvas.configure(bg=T["panel"], highlightbackground=T["sep"])
        self._hdr.configure(bg=T["hdr_bg"])
        self._hdr_lbl.configure(bg=T["hdr_bg"], fg=T["hdr_fg"])
        self._hdr_sub.configure(bg=T["hdr_bg"], fg=T["hdr_fg"])
        self._theme_btn.configure(bg=T["hdr_bg"], fg=T["hdr_fg"], activebackground=T["btn"],
                                  activeforeground=T["fg"], text="Hell" if dark else "Dark")
        self.log_box.configure(bg=T["log_bg"], fg=T["log_fg"], insertbackground=T["log_fg"])
        self.lineid_w.apply_theme(T)
        self._update_osgeo_label()
        self._update_staging_label()
        for group, key in ((self._dim_labels, "fg_dim"), (self._accent_labels, "accent"),
                           (self._hint_labels, "hint"), (self._err_labels, "err")):
            for lbl in group:
                try:
                    lbl.configure(foreground=T[key])
                except tk.TclError:
                    pass
        self._update_start_btn_state()
        _set_titlebar_dark(self, dark)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DSMDiffApp()
    app.mainloop()
