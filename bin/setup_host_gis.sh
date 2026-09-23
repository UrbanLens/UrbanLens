#!/usr/bin/env bash
#
# Make GeoDjango importable on this host without root, and check that it is.
#
# settings/_gdal_local.py points Django at the GDAL and GEOS vendored in the pyogrio and shapely
# wheels whenever the system has neither, so the install step is `uv sync`. The GDAL version is
# whatever pyogrio's wheel bundles, pinned through pyogrio in pyproject.toml. Installing the system
# libraries (`sudo apt install libgdal-dev`) makes the fallback stand aside.
#
# Safe to rerun. Prints the host's GDAL/GEOS beside the app container's, when it is running.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync

DJANGO_SETTINGS_MODULE=urbanlens.UrbanLens.settings.test uv run python - <<'PY'
import django

django.setup()

from django.conf import settings
from django.contrib.gis.gdal import gdal_version
from django.contrib.gis.geos import GEOSGeometry, geos_version

point = GEOSGeometry("POINT(-73.98 40.75)", srid=4326)
point.transform(3857)
source = getattr(settings, "GDAL_LIBRARY_PATH", None) or "system libraries"
print(f"host:      GDAL {gdal_version().decode()}, GEOS {geos_version().decode()} ({source})")
PY

name=$(sed -n 's/^UL_CONTAINER_NAME=//p' .env 2>/dev/null | tr -d "\"'" | head -1)
app="urbanlens_${name}_app"
if [ -n "$name" ] && docker inspect "$app" >/dev/null 2>&1; then
    echo "container: GDAL $(docker exec "$app" gdal-config --version), GEOS $(docker exec "$app" geos-config --version) ($app)"
fi
