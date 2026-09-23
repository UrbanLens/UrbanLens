"""Tests for the wheel-vendored GDAL/GEOS fallback in the local and test settings."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest import mock

from django.test import SimpleTestCase

from urbanlens.UrbanLens.settings import _gdal_local
from urbanlens.UrbanLens.settings._gdal_local import local_gdal_overrides

_SRC = Path(_gdal_local.__file__).resolve().parents[2].parent


class LocalGdalOverridesTests(SimpleTestCase):
    def test_system_libraries_are_left_to_django(self) -> None:
        with mock.patch.object(_gdal_local, "find_library", return_value="libgdal.so.39"):
            self.assertEqual(local_gdal_overrides(), {})

    def test_a_host_without_system_libraries_gets_the_wheel_copies(self) -> None:
        # The environment and loader are patched so the suite's own GDAL/PROJ stay untouched.
        with (
            mock.patch.object(_gdal_local, "find_library", return_value=None),
            mock.patch.object(_gdal_local.ctypes, "CDLL"),
            mock.patch.dict("os.environ"),
        ):
            overrides = local_gdal_overrides()

        self.assertIn("pyogrio.libs", overrides["GDAL_LIBRARY_PATH"])
        self.assertIn("shapely.libs", overrides["GEOS_LIBRARY_PATH"])
        self.assertTrue(Path(overrides["GDAL_LIBRARY_PATH"]).is_file())
        self.assertTrue(Path(overrides["GEOS_LIBRARY_PATH"]).is_file())

    def test_geodjango_loads_the_wheel_copies(self) -> None:
        """Runs in a fresh interpreter, so the wheel libraries never load into the suite's own process."""
        probe = textwrap.dedent(
            """
            from unittest import mock
            from django.conf import settings
            from urbanlens.UrbanLens.settings import _gdal_local

            with mock.patch.object(_gdal_local, "find_library", return_value=None):
                overrides = _gdal_local.local_gdal_overrides()
            settings.configure(**overrides)

            from django.contrib.gis.gdal import OGRGeometry, gdal_version
            from django.contrib.gis.geos import GEOSGeometry, geos_version

            point = GEOSGeometry("POINT(-73.98 40.75)", srid=4326)
            point.transform(3857)
            assert round(point.x) == -8235416, point.wkt
            assert OGRGeometry("POINT(1 2)").wkt == "POINT (1 2)"
            print(gdal_version().decode(), geos_version().decode())
            """,
        )
        env = {**os.environ, "PYTHONPATH": str(_SRC)}
        env.pop("DJANGO_SETTINGS_MODULE", None)

        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120, check=False
        )

        self.assertEqual(result.returncode, 0, result.stderr)
