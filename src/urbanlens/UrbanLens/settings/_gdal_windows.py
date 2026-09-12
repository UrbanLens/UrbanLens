"""Windows-only GeoDjango GDAL/GEOS wiring for local dev.

Points at DLLs vendored by pyogrio/shapely; Linux/Docker/CI never hit this path.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
import sys


def local_windows_gdal_overrides() -> dict[str, str]:
    """Locate vendored GDAL/GEOS DLLs for local Windows dev.

    Returns:
        Overrides for settings globals; empty outside local Windows dev.
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
        # Registers pyogrio's DLL dir so the GDAL DLL resolves its deps.
        import pyogrio

        pyogrio_dir = Path(pyogrio.__file__).parent
        os.environ.setdefault("GDAL_DATA", str(pyogrio_dir / "gdal_data"))
        os.environ.setdefault("PROJ_LIB", str(pyogrio_dir / "proj_data"))
    except ImportError:
        pass

    return overrides
