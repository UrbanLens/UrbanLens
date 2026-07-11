from __future__ import annotations

from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

globals().update(local_windows_gdal_overrides())
