"""
_stac_http.py - Netzwerk fuer den STAC-Zugriff: Credentials, Proxy, Multipart-Upload.

KOPIEN / AUSZUEGE aus topo-rapidmapping-int, Stand 2026-10-20:
  utilities/credentials.py          -> load_stac_credentials
  utilities/proxy_handler.py        -> initialize_proxy, get_session, get_proxy_config
                                       (nur Modus 'auto': direkt -> System-Proxy inkl.
                                       Kerberos -> proxy_config.json)
  main_multipart_upload_via_api.py  -> multipart_upload, TimeoutHTTPAdapter, HttpError

Bewusste Abweichungen zum Original:
  - Die Pfade zu stac_credentials.json / proxy_config.json kommen als Argument (Secrets-
    Ordner aus dem GUI), nicht relativ zum aktuellen Arbeitsverzeichnis.
  - Ohne proxy_config.json gibt es keinen Platzhalter-Proxy (das Original testete dann
    'http://My.proxy.ch:8080').
  - multipart_upload ist eine Funktion ohne den sys.argv-Umweg. Die Pruefsumme kommt aus
    hashlib ("1220" + sha256.hexdigest()) - identisch zum multihash-Paket, das im
    QGIS-Python fehlt.
  - Abbruch ueber cancel() zwischen den Teilen; der Upload wird dann serverseitig
    abgebrochen (abort), es bleibt kein halbes Asset zurueck.
  - Credentials werden nie geloggt und stehen in keiner Fehlermeldung. Auch die
    vorsignierten S3-URLs (temporaere Zugriffsrechte) werden nicht geloggt.
"""

import base64
import hashlib
import json
import logging
import math
import os
import threading
import time
import urllib.request
from pathlib import Path

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = "stac_credentials.json"
PROXY_FILE = "proxy_config.json"

# Grosse Teile ueber VPN brauchen Zeit (Original: 60 -> 180 s)
DEFAULT_TIMEOUT = 180
# STAC-API: hoechstens 100 Teile pro Upload. Mit 250 MB pro Teil reicht das bis ~24 GB,
# darueber wird die Teilgroesse so gewaehlt, dass ~90 Teile entstehen (Reserve).
DEFAULT_PART_SIZE_MB = 250
MAX_PARTS_NUMBER = 100
MULTIPART_PARTS_TARGET = 90


# ─── Credentials (aus utilities/credentials.py) ───────────────────────────────
def load_stac_credentials(secrets_dir, environment):
    """(username, password, hostname_aus_datei) fuer 'INT' oder 'PROD'.

    Prioritaet wie im Original: Umgebungsvariablen STAC_USERNAME/STAC_PASSWORD, sonst
    <secrets_dir>/stac_credentials.json. Der Hostname aus der Datei wird nur
    zurueckgegeben (Vergleich in stac_import) - welcher Server angesprochen wird,
    bestimmt stac_import.STAC_HOSTS.

    Fehlermeldungen nennen nur Pfad und Schluessel, nie Inhalte der Datei."""
    environment = str(environment).upper()
    path = Path(secrets_dir) / CREDENTIALS_FILE if secrets_dir else None

    env_cfg = None
    if path is not None and path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Credentials-Datei ist kein gueltiges JSON (Zeile {e.lineno}): "
                             f"{path}") from None
        except OSError as e:
            raise ValueError(f"Credentials-Datei nicht lesbar: {path} ({e.strerror})") from None
        if isinstance(data, dict) and isinstance(data.get(environment), dict):
            env_cfg = data[environment]
    hostname = env_cfg.get("hostname") if env_cfg else None

    user, password = os.environ.get("STAC_USERNAME"), os.environ.get("STAC_PASSWORD")
    if user and password:
        logger.info(f"Credentials: Umgebungsvariablen STAC_USERNAME/STAC_PASSWORD ({environment})")
        return user, password, hostname

    if path is None or not path.is_file():
        raise ValueError(
            f"Keine STAC-Credentials fuer {environment} gefunden.\n"
            f"  Erwartet: {path or '<Secrets-Ordner>' + os.sep + CREDENTIALS_FILE}\n"
            f"  Vorlage: proxy_credentials\\stac_credentials.examples im Projektordner\n"
            f"  Alternative: Umgebungsvariablen STAC_USERNAME und STAC_PASSWORD")
    if env_cfg is None:
        raise ValueError(f"Abschnitt '{environment}' fehlt in {path} "
                         f"(Aufbau siehe proxy_credentials\\stac_credentials.examples).")
    user, password = env_cfg.get("username"), env_cfg.get("password")
    if not user or not password:
        raise ValueError(f"'username' oder 'password' fehlt im Abschnitt '{environment}' "
                         f"von {path}.")
    logger.info(f"Credentials: {path} ({environment})")
    return user, password, hostname


# ─── Proxy (aus utilities/proxy_handler.py, nur Modus 'auto') ─────────────────
PROXY_CONFIG = {
    "enabled": False,
    "proxies": None,
    "session": None,
    "verify_ssl": True,
    "active_proxy": None,
    "initialized": False,
    "is_vpn": False,
    "auth_method": None,
}
_PROXY_INIT_LOCK = threading.Lock()

DEFAULT_PROXY_CONFIG = {
    "proxies": [],
    "test_url": "https://data.geo.admin.ch/browser/index.html",
    "timeout": 5,
    "disable_ssl_warnings": True,
}


def load_proxy_config(proxy_config_path=None):
    """Proxy-Konfiguration aus <secrets>/proxy_config.json, sonst Default (keine Proxies)."""
    path = Path(proxy_config_path) if proxy_config_path else None
    if path is None or not path.is_file():
        logger.info(f"  Keine Proxy-Config ({path or PROXY_FILE}) - nur direkt / System-Proxy")
        return DEFAULT_PROXY_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        logger.info(f"  Proxy-Config geladen: {path}")
        return config
    except Exception as e:
        logger.warning(f"Proxy-Config nicht lesbar ({path}): {e} - verwende Default")
        return DEFAULT_PROXY_CONFIG


def _test_connection(test_url, proxies=None, verify_ssl=True, timeout=5):
    try:
        r = requests.get(test_url, proxies=proxies, verify=verify_ssl, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _detect_vpn_connection(proxies, test_urls):
    """VPN mit SSL-Inspection: Verbindung geht ueber den Proxy nur ohne SSL-Pruefung."""
    if not proxies:
        return False
    for url in test_urls:
        with_ssl = _test_connection(url, proxies=proxies, verify_ssl=True, timeout=3)
        without_ssl = _test_connection(url, proxies=proxies, verify_ssl=False, timeout=3)
        if without_ssl and not with_ssl:
            logger.info(f"  VPN mit SSL-Inspection erkannt (getestet mit {url})")
            return True
    return False


def _get_system_proxy_urls():
    """Proxy-URLs aus Windows-Registry / Umgebungsvariablen (inkl. GDAL_HTTP_PROXY),
    dedupliziert, https zuerst."""
    raw = urllib.request.getproxies()
    keys = ("https_proxy", "http_proxy", "all_proxy", "HTTPS_PROXY", "HTTP_PROXY",
            "ALL_PROXY", "GDAL_HTTP_PROXY", "GDAL_HTTPS_PROXY",
            "gdal_http_proxy", "gdal_https_proxy")
    seen, result = set(), []
    for key in ("https", "http", "gdal_https", "gdal_http"):
        url = raw.get(key)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    for key in keys:
        url = os.environ.get(key)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _make_kerberos_session(proxies_dict, verify_ssl):
    """Session mit Kerberos/SSPI-Proxy-Authentifizierung (pyspnego, sonst
    requests-negotiate-sspi). (None, None), wenn keine der Bibliotheken installiert ist."""
    proxy_host = ""
    if proxies_dict:
        from urllib.parse import urlparse
        proxy_host = urlparse(next(iter(proxies_dict.values()), "")).hostname or ""

    try:
        import spnego

        class _SpnegoProxyAuth(requests.auth.AuthBase):
            def __call__(self, r):
                ctx = spnego.client(hostname=proxy_host, service="http", protocol="negotiate")
                out = ctx.step()
                if out:
                    r.headers["Proxy-Authorization"] = f"Negotiate {base64.b64encode(out).decode()}"
                return r

        session = requests.Session()
        if proxies_dict:
            session.proxies.update(proxies_dict)
        session.verify = verify_ssl
        session.auth = _SpnegoProxyAuth()
        return session, "spnego"
    except ImportError:
        pass

    try:
        from requests_negotiate_sspi import HttpNegotiateAuth
        session = requests.Session()
        if proxies_dict:
            session.proxies.update(proxies_dict)
        session.verify = verify_ssl
        session.auth = HttpNegotiateAuth()
        return session, "negotiate-sspi"
    except ImportError:
        pass
    return None, None


def _install_kerberos_tunnel_patch():
    """HTTPS durch einen Kerberos-Proxy: der Negotiate-Token wird schon in den CONNECT-
    Request gesetzt, damit aeltere urllib3 nicht an der 407-Antwort scheitern.
    Idempotent; ohne Kerberos-Bibliothek faellt der Tunnel auf unauthentifiziert zurueck."""
    import http.client

    if getattr(http.client.HTTPConnection, "_kerberos_patched", False):
        return
    orig_tunnel = http.client.HTTPConnection._tunnel

    def _negotiate_token(proxy_host):
        try:
            import spnego
            out = spnego.client(hostname=proxy_host, service="http", protocol="negotiate").step()
            if out:
                return base64.b64encode(out).decode()
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"SSPI pyspnego: {type(e).__name__}: {e}")
        try:
            import sspi
            auth = sspi.ClientAuth("Negotiate", targetspn=f"HTTP/{proxy_host}")
            _, out_buf = auth.authorize(None)
            return base64.b64encode(out_buf[0].Buffer).decode()
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"SSPI pywin32: {type(e).__name__}: {e}")
        return ""

    def _sspi_tunnel(self):
        token = _negotiate_token(self.host)
        if not token:
            return orig_tunnel(self)
        saved = self._tunnel_headers.pop("Proxy-Authorization", None)
        self._tunnel_headers["Proxy-Authorization"] = f"Negotiate {token}"
        try:
            return orig_tunnel(self)
        finally:
            self._tunnel_headers.pop("Proxy-Authorization", None)
            if saved is not None:
                self._tunnel_headers["Proxy-Authorization"] = saved

    http.client.HTTPConnection._tunnel = _sspi_tunnel
    http.client.HTTPConnection._kerberos_patched = True


def _probe_proxy(proxy_url, test_url, timeout):
    proxies = {"http": proxy_url, "https": proxy_url}
    for verify in (True, False):
        try:
            r = requests.get(test_url, proxies=proxies, verify=verify, timeout=timeout)
            if r.status_code == 200:
                return {"status": "ok", "verify_ssl": verify}
        except requests.exceptions.ProxyError as e:
            if "407" in str(e):
                return {"status": "needs_kerberos"}
        except Exception:
            pass
    return {"status": "fail"}


def _probe_proxy_kerberos(proxy_url, test_url, timeout):
    proxies = {"http": proxy_url, "https": proxy_url}
    for verify in (True, False):
        session, method = _make_kerberos_session(proxies, verify)
        if session is None:
            return {"status": "no_lib"}
        try:
            r = session.get(test_url, timeout=timeout)
            if r.status_code == 200:
                return {"status": "ok", "verify_ssl": verify, "session": session, "method": method}
        except Exception:
            pass
    return {"status": "fail"}


def _ok(name, proxies_dict, verify, session, is_vpn=False, auth_method=None):
    return {"enabled": proxies_dict is not None, "proxies": proxies_dict, "session": session,
            "verify_ssl": verify, "active_proxy": name, "initialized": True,
            "is_vpn": is_vpn, "auth_method": auth_method}


def _connect_named_proxy(name, proxy_url, test_url, timeout):
    probe = _probe_proxy(proxy_url, test_url, timeout)
    proxies = {"http": proxy_url, "https": proxy_url}
    if probe["status"] == "ok":
        is_vpn = _detect_vpn_connection(
            proxies, [test_url, "https://sys-data.int.bgdi.ch/api/stac/v0.9/"])
        use_ssl = probe["verify_ssl"] and not is_vpn
        s = requests.Session()
        s.proxies.update(proxies)
        s.verify = use_ssl
        logger.info(f"  Proxy '{name}' OK" + (" (VPN, SSL-Pruefung aus)" if is_vpn else
                                              "" if use_ssl else " (SSL-Pruefung aus)"))
        _install_kerberos_tunnel_patch()
        return _ok(name, proxies, use_ssl, s, is_vpn)
    if probe["status"] == "needs_kerberos":
        k = _probe_proxy_kerberos(proxy_url, test_url, timeout)
        if k["status"] == "ok":
            logger.info(f"  Proxy '{name}' OK (Kerberos/{k['method']})")
            _install_kerberos_tunnel_patch()
            return _ok(f"{name} (kerberos/{k['method']})", proxies, k["verify_ssl"],
                       k["session"], auth_method=f"kerberos/{k['method']}")
        if k["status"] == "no_lib":
            logger.warning(f"Proxy '{name}' verlangt Kerberos, aber pyspnego bzw. "
                           f"requests-negotiate-sspi fehlt im OSGeo4W-Python.")
    else:
        logger.info(f"  Proxy '{name}' nicht erreichbar")
    return None


def _detect_proxy_requirement(proxy_config_path):
    """Reihenfolge wie im Original (Modus 'auto'): direkt -> System-Proxy (Kerberos
    zuerst, dann ohne Auth) -> Proxies aus proxy_config.json. ConnectionError, wenn
    nichts geht."""
    logger.info("Netzwerk: Verbindung wird getestet ...")
    config = load_proxy_config(proxy_config_path)
    test_url = config.get("test_url", DEFAULT_PROXY_CONFIG["test_url"])
    timeout = config.get("timeout", DEFAULT_PROXY_CONFIG["timeout"])
    if config.get("disable_ssl_warnings", True):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if _test_connection(test_url, proxies=None, verify_ssl=True, timeout=timeout):
        logger.info("  Direkte Verbindung OK (kein Proxy)")
        s = requests.Session()
        s.verify = True
        return _ok(None, None, True, s)

    for proxy_url in _get_system_proxy_urls():
        proxies = {"http": proxy_url, "https": proxy_url}
        session, method = _make_kerberos_session(proxies, verify_ssl=False)
        if session is not None:
            try:
                if session.get(test_url, timeout=timeout).status_code == 200:
                    logger.info(f"  System-Proxy OK (Kerberos/{method}): {proxy_url}")
                    _install_kerberos_tunnel_patch()
                    return _ok(f"system:{proxy_url}", proxies, False, session,
                               auth_method=f"kerberos/{method}")
            except Exception:
                pass
        probe = _probe_proxy(proxy_url, test_url, timeout)
        if probe["status"] == "ok":
            s = requests.Session()
            s.proxies.update(proxies)
            s.verify = probe["verify_ssl"]
            logger.info(f"  System-Proxy OK: {proxy_url}")
            _install_kerberos_tunnel_patch()
            return _ok(f"system:{proxy_url}", proxies, probe["verify_ssl"], s,
                       not probe["verify_ssl"])
        if probe["status"] == "needs_kerberos":
            logger.warning(f"System-Proxy {proxy_url}: 407 und Kerberos nicht moeglich "
                           f"(pyspnego im OSGeo4W-Python installieren).")

    enabled = [p for p in config.get("proxies", []) if p.get("enabled", True)]
    for p in enabled:
        if not p.get("url"):
            continue
        result = _connect_named_proxy(p.get("name", "Proxy"), p["url"], test_url, timeout)
        if result is not None:
            return result

    raise ConnectionError(
        "Keine Internet-Verbindung moeglich (direkt, System-Proxy"
        + (f", {len(enabled)} Proxy(s) aus {proxy_config_path}" if enabled else "")
        + f").\nTest-URL: {test_url}\nNetzwerk oder Proxy-Config pruefen.")


def initialize_proxy(proxy_config_path=None):
    """Richtet die Verbindung einmal pro Prozess ein (thread-sicher)."""
    if PROXY_CONFIG.get("initialized"):
        return
    with _PROXY_INIT_LOCK:
        if PROXY_CONFIG.get("initialized"):
            return
        PROXY_CONFIG.update(_detect_proxy_requirement(proxy_config_path))
        session = PROXY_CONFIG["session"]
        adapter = TimeoutHTTPAdapter(max_retries=_retry(3, 1), timeout=60)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        logger.info("Netzwerk: " + (f"Proxy {PROXY_CONFIG['active_proxy']}"
                                     if PROXY_CONFIG["enabled"] else "direkte Verbindung"))


def get_session(proxy_config_path=None):
    """requests-Session mit Proxy-/SSL-Einstellung, Wiederholung und Timeout."""
    if not PROXY_CONFIG.get("initialized"):
        initialize_proxy(proxy_config_path)
    return PROXY_CONFIG["session"]


def get_proxy_config():
    return PROXY_CONFIG.copy()


# ─── Multipart-Upload (aus main_multipart_upload_via_api.py) ──────────────────
class TimeoutHTTPAdapter(HTTPAdapter):
    """Session-weiter Timeout fuer requests."""

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


class HttpError(RuntimeError):
    """HTTP-Fehler mit Status, URL und der 'description' der STAC-Antwort."""

    def __init__(self, response, message=None):
        msg = f"HTTP {response.status_code} fuer {response.url}"
        try:
            msg += f" - {json.dumps(response.json()['description'])}"
        except Exception:
            pass
        if message:
            msg += f": {message}"
        super().__init__(msg)
        self.response = response


def _retry(total, backoff):
    return Retry(total=total, backoff_factor=backoff,
                 status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)


def make_upload_sessions(proxy_cfg=None):
    """Zwei Sessions wie im Original: API (mit Proxy) und S3 fuer die vorsignierten
    URLs. Hinter dem Firmen-Proxy muss auch S3 ueber den Proxy, dort ohne SSL-Pruefung
    (SSL-Inspection); ohne Proxy direkt mit SSL-Pruefung."""
    proxy_cfg = proxy_cfg or {}
    use_proxy = bool(proxy_cfg.get("enabled") and proxy_cfg.get("proxies"))

    api = requests.Session()
    if use_proxy:
        api.proxies.update(proxy_cfg["proxies"])
        api.verify = proxy_cfg.get("verify_ssl") is not False
    a = TimeoutHTTPAdapter(max_retries=_retry(3, 1), timeout=DEFAULT_TIMEOUT)
    api.mount("http://", a)
    api.mount("https://", a)

    s3 = requests.Session()
    if use_proxy:
        s3.proxies.update(proxy_cfg["proxies"])
        s3.verify = False
    if not api.verify or not s3.verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    b = TimeoutHTTPAdapter(max_retries=_retry(5, 2), timeout=DEFAULT_TIMEOUT)
    s3.mount("http://", b)
    s3.mount("https://", b)
    return api, s3


def part_size_mb_for(file_size_bytes):
    """Teilgroesse in MB, so dass hoechstens ~90 Teile entstehen (API-Limit 100)."""
    return max(DEFAULT_PART_SIZE_MB, math.ceil(file_size_bytes / 1048576 / MULTIPART_PARTS_TARGET))


def sha256_multihash(path, chunk_mb=16):
    """checksum:multihash (sha2-256) einer Datei: '1220' + sha256.hexdigest().
    Gegen das multihash-Paket geprueft: identisch (0x12 = sha2-256, 0x20 = 32 Byte)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_mb * 1048576), b""):
            h.update(chunk)
    return "1220" + h.hexdigest()


def same_multihash(a, b):
    """STAC liefert die Pruefsumme in Grossbuchstaben - immer ohne Gross/Klein vergleichen."""
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_CHUNK = 4 * 1048576


def download_file(session, href, dest, expected="", cancel=None, progress=None,
                  attempts=DOWNLOAD_ATTEMPTS):
    """Laedt href nach dest (zuerst dest.part, erst nach bestandener Pruefsumme os.replace).

    expected = checksum:multihash aus dem STAC (sha2-256), Vergleich ohne Gross/Klein.
    Liegt dest schon mit passender Pruefsumme da, wird nichts geladen.
    progress(anteil 0..1, text) optional, cancel() -> True bricht ab.
    Gibt 'cache' oder 'geladen' zurueck."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and expected and same_multihash(sha256_multihash(dest), expected):
        return "cache"
    part = Path(str(dest) + ".part")
    last = None
    for attempt in range(1, attempts + 1):
        try:
            sha = hashlib.sha256()
            done = 0
            with session.get(href, stream=True) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                total = int(r.headers.get("Content-Length") or 0)
                with open(part, "wb") as f:
                    for chunk in r.iter_content(DOWNLOAD_CHUNK):
                        if cancel is not None and cancel():
                            raise InterruptedError("Abbruch durch den Benutzer (Download)")
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(done / total, f"Download {dest.name}: {done / 1048576:.0f}"
                                                   f"/{total / 1048576:.0f} MB")
            got = "1220" + sha.hexdigest()
            if expected and not same_multihash(got, expected):
                raise RuntimeError(f"Pruefsumme stimmt nicht (lokal {got[:16]}…, "
                                   f"STAC {str(expected)[:16]}…)")
            if not expected:
                logger.warning(f"{dest.name}: STAC nennt keine Pruefsumme - nicht geprueft.")
            os.replace(part, dest)
            return "geladen"
        except InterruptedError:
            _remove_quiet(part)
            raise
        except Exception as e:
            last = e
            _remove_quiet(part)
            if attempt < attempts:
                logger.warning(f"Download {dest.name}: {e} - neuer Versuch ({attempt}/{attempts})")
                time.sleep(2 * attempt)
    raise RuntimeError(f"Download nach {attempts} Versuchen fehlgeschlagen: {href} ({last})")


def _remove_quiet(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _file_hashes(path, part_size, cancel=None):
    """(checksum_multihash, md5_parts) in einem Durchgang ueber die Datei."""
    sha = hashlib.sha256()
    md5_parts = []
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel():
                raise InterruptedError("Abbruch durch den Benutzer (Pruefsummen)")
            data = f.read(part_size)
            if not data:
                break
            sha.update(data)
            md5_parts.append({"part_number": len(md5_parts) + 1,
                              "md5": base64.b64encode(hashlib.md5(data).digest()).decode()})
    return "1220" + sha.hexdigest(), md5_parts


def multipart_upload(api_session, s3_session, uploads_url, filepath, auth,
                     part_size_mb=None, force=True, cancel=None, progress=None):
    """Laedt filepath als Multipart-Upload zu einem bereits angelegten Asset hoch.

    uploads_url: .../collections/<c>/items/<i>/assets/<a>/uploads
    progress(anteil 0..1, text) optional, cancel() -> True bricht ab.
    Gibt die lokal berechnete checksum:multihash zurueck. Bei jedem Fehler oder Abbruch
    nach dem Anlegen wird der Upload serverseitig abgebrochen."""
    size = os.path.getsize(filepath)
    part_mb = part_size_mb or part_size_mb_for(size)
    part_size = part_mb * 1048576
    n_parts = max(1, math.ceil(size / part_size))
    if n_parts >= MAX_PARTS_NUMBER:
        raise ValueError(f"{n_parts} Teile - das STAC-API erlaubt hoechstens {MAX_PARTS_NUMBER}.")

    logger.info(f"  Pruefsummen: {size / 1048576:.1f} MB in {n_parts} Teil(en) a {part_mb} MB")
    checksum, md5_parts = _file_hashes(filepath, part_size, cancel)

    if force:
        r = api_session.get(uploads_url, params={"status": "in-progress"}, auth=auth)
        if r.status_code == 200:
            for up in r.json().get("uploads", []):
                logger.warning(f"Laufender Upload {up.get('upload_id')} wird abgebrochen (force).")
                _abort(api_session, uploads_url, up["upload_id"], auth)

    r = api_session.post(uploads_url, auth=auth, json={
        "number_parts": len(md5_parts), "md5_parts": md5_parts,
        "checksum:multihash": checksum, "update_interval": 30})
    if r.status_code != 201:
        raise HttpError(r, "Multipart-Upload konnte nicht angelegt werden")
    upload_id, urls = r.json()["upload_id"], r.json()["urls"]

    try:
        parts = []
        started = time.time()
        with open(filepath, "rb") as f:
            for url in sorted(urls, key=lambda u: u["part"]):
                if cancel is not None and cancel():
                    raise InterruptedError("Abbruch durch den Benutzer (Upload)")
                f.seek((url["part"] - 1) * part_size)
                data = f.read(part_size)
                md5 = md5_parts[url["part"] - 1]["md5"]
                etag = _put_part(s3_session, url, data, md5)
                parts.append({"etag": etag, "part_number": url["part"]})
                done = len(parts) / len(urls)
                rate = (done * size / 1048576) / max(time.time() - started, 1e-6)
                logger.info(f"  Teil {url['part']}/{len(urls)} hochgeladen ({rate:.1f} MB/s)")
                if progress:
                    progress(done, f"Upload Teil {url['part']}/{len(urls)}")
        r = api_session.post(f"{uploads_url}/{upload_id}/complete", auth=auth,
                             json={"parts": parts})
        if r.status_code != 200 or r.json().get("status") != "completed":
            raise HttpError(r, "Upload konnte nicht abgeschlossen werden")
    except BaseException:
        try:
            _abort(api_session, uploads_url, upload_id, auth)
            logger.warning(f"Upload {upload_id} serverseitig abgebrochen.")
        except Exception as e:
            logger.error(f"Upload {upload_id} liess sich nicht abbrechen: {e}")
        raise
    return checksum


def _put_part(s3_session, url, data, md5, attempts=5):
    """Ein Teil an die vorsignierte URL; 5 Versuche mit wachsender Wartezeit."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            r = s3_session.put(url["url"], data=data, headers={"Content-MD5": md5},
                               timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                return r.headers["ETag"]
            last = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            last = type(e).__name__
        if attempt < attempts:
            wait = 2 * attempt
            logger.warning(f"Teil {url['part']}: {last} - neuer Versuch in {wait} s "
                           f"({attempts - attempt} uebrig)")
            time.sleep(wait)
    raise RuntimeError(f"Teil {url['part']} nach {attempts} Versuchen nicht hochgeladen ({last}).")


def _abort(api_session, uploads_url, upload_id, auth):
    r = api_session.post(f"{uploads_url}/{upload_id}/abort", auth=auth)
    if r.status_code != 200 or r.json().get("status") != "aborted":
        raise HttpError(r, "Abbrechen des Uploads fehlgeschlagen")
