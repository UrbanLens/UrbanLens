"""Windows-only GeoDjango GDAL/GEOS wiring for local dev (see CLAUDE.md).

Windows dev machines have no system GDAL/GEOS install - GeoDjango's postgis
backend needs GDAL_LIBRARY_PATH/GEOS_LIBRARY_PATH pointed at the copies
vendored by geopandas' pyogrio/shapely dependencies instead. This only
applies to local dev (UL_ENVIRONMENT unset or "local") - Docker, CI, and
production all run Linux with a real GDAL/GEOS install and must never hit
this path.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
import sys


def local_windows_gdal_overrides() -> dict[str, str]:
    """Locate the GDAL/GEOS DLLs vendored by pyogrio/shapely for local Windows dev.

    Returns:
        dict[str, str]: GDAL_LIBRARY_PATH/GEOS_LIBRARY_PATH overrides to splat into
        a settings module's globals(). Empty outside of local Windows dev, or if the
        vendored DLLs can't be found.
    """
    if os.name != "nt" or os.getenv("UL_ENVIRONMENT", "local").lower() != "local":
        return {}

    venv_libs = Path(sys.prefix) / "Lib" / "site-packages"

    def _find(*patterns: str) -> str | None:
        for libs_dir in ("pyogrio.libs", "shapely.libs"):
            for pattern in patterns:
                matches = glob.glob(str(venv_libs / libs_dir / pattern))
                if matches:
                    return matches[0]
        return None

    overrides: dict[str, str] = {}
    if gdal_dll := _find("gdal-*.dll"):
        overrides["GDAL_LIBRARY_PATH"] = gdal_dll
    if geos_dll := _find("geos_c-*.dll", "libgeos_c-*.dll"):
        overrides["GEOS_LIBRARY_PATH"] = geos_dll

    try:
        # Importing pyogrio registers its bundled DLL directory with the
        # process (via os.add_dll_directory), which is what lets the GDAL DLL
        # above resolve its own dependencies (PROJ, SQLite, etc.) at load time.
        import pyogrio

        pyogrio_dir = Path(pyogrio.__file__).parent
        os.environ.setdefault("GDAL_DATA", str(pyogrio_dir / "gdal_data"))
        os.environ.setdefault("PROJ_LIB", str(pyogrio_dir / "proj_data"))
    except ImportError:
        pass

    return overrides
