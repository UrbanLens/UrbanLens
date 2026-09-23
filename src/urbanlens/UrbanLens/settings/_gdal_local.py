"""GeoDjango GDAL/GEOS wiring for a host without system GDAL/GEOS.

Falls back to the copies pyogrio and shapely vendor in their wheels. Docker images install
``libgdal-dev``, so the system libraries are found there and this returns nothing.
"""

from __future__ import annotations

import ctypes
from ctypes.util import find_library
from importlib.util import find_spec
import os
from pathlib import Path


def _package_dir(name: str) -> Path | None:
    spec = find_spec(name)
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).parent


def _vendored(package: str, *patterns: str) -> Path | None:
    """Return the first library in ``<package>.libs`` matching a pattern, without importing it."""
    package_dir = _package_dir(package)
    if package_dir is None:
        return None
    libs_dir = package_dir.parent / f"{package}.libs"
    for pattern in patterns:
        if matches := sorted(libs_dir.glob(pattern)):
            return matches[0]
    return None


def _point_gdal_at_wheel_data() -> None:
    if pyogrio_dir := _package_dir("pyogrio"):
        os.environ.setdefault("GDAL_DATA", str(pyogrio_dir / "gdal_data"))
        os.environ.setdefault("PROJ_DATA", str(pyogrio_dir / "proj_data"))
        os.environ.setdefault("PROJ_LIB", str(pyogrio_dir / "proj_data"))


def _windows_overrides() -> dict[str, str]:
    if os.getenv("UL_ENVIRONMENT", "local").lower() != "local":
        return {}

    overrides: dict[str, str] = {}
    if gdal_dll := _vendored("pyogrio", "gdal-*.dll"):
        overrides["GDAL_LIBRARY_PATH"] = str(gdal_dll)
    if geos_dll := _vendored("shapely", "geos_c-*.dll", "libgeos_c-*.dll"):
        overrides["GEOS_LIBRARY_PATH"] = str(geos_dll)
    if overrides:
        _point_gdal_at_wheel_data()
        # Importing pyogrio registers its DLL directory, so the GDAL DLL resolves its own deps.
        import pyogrio  # noqa: F401
    return overrides


def _posix_overrides() -> dict[str, str]:
    overrides: dict[str, str] = {}
    if find_library("gdal") is None and (gdal_so := _vendored("pyogrio", "libgdal-*.so*")):
        overrides["GDAL_LIBRARY_PATH"] = str(gdal_so)
        _point_gdal_at_wheel_data()
    if find_library("geos_c") is None and (geos_c_so := _vendored("shapely", "libgeos_c-*.so*")):
        # libgeos_c carries no RUNPATH to its sibling libgeos; loading that first lets the
        # dynamic linker satisfy the dependency by soname when Django opens libgeos_c.
        if geos_so := _vendored("shapely", "libgeos-*.so*"):
            ctypes.CDLL(str(geos_so), mode=ctypes.RTLD_GLOBAL)
        overrides["GEOS_LIBRARY_PATH"] = str(geos_c_so)
    return overrides


def local_gdal_overrides() -> dict[str, str]:
    """Locate GDAL/GEOS for a dev host that lacks the system libraries.

    Returns:
        Overrides for settings globals; empty where the system libraries are installed.
    """
    return _windows_overrides() if os.name == "nt" else _posix_overrides()
