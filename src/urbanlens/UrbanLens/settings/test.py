from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

TESTING = True

globals().update(local_windows_gdal_overrides())
