from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

TESTING = True

# model_bakery's default related-object generation collides with the
# create_user_profile post_save signal (see urbanlens.core.tests.baker).
BAKER_CUSTOM_CLASS = "urbanlens.core.tests.baker.SignalSafeBaker"

globals().update(local_windows_gdal_overrides())
