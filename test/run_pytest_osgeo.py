"""
run_pytest_osgeo.py - pytest im OSGeo4W/QGIS-Python starten (dort fehlt pytest).

Aufruf, nachdem o4w_env.bat der QGIS-Installation geladen ist:
    python test\\run_pytest_osgeo.py <site-packages mit pytest> [pytest-Argumente]
z.B.
    call "C:\\Program Files\\QGIS 3.44.15\\bin\\o4w_env.bat"
    python test\\run_pytest_osgeo.py C:\\Python314\\Lib\\site-packages -q

Die fremden site-packages kommen HINTEN an sys.path: so gewinnen numpy/osgeo des
QGIS-Python, und nur pytest (reines Python) kommt aus der anderen Installation.
Alternativ: Umgebungsvariable PYTEST_SITE_PACKAGES statt des ersten Arguments.
"""

import os
import sys

if __name__ == "__main__":
    args = sys.argv[1:]
    site = os.environ.get("PYTEST_SITE_PACKAGES")
    if args and os.path.isdir(args[0]):
        site = args.pop(0)
    if not site or not os.path.isdir(site):
        sys.exit("site-packages mit pytest angeben (Argument oder PYTEST_SITE_PACKAGES).")
    sys.path.append(site)
    import pytest
    here = os.path.dirname(os.path.abspath(__file__))
    sys.exit(pytest.main(args or ["-q", here]))
