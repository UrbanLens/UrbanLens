from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.app import settings as _app_settings
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

TESTING = True

# No clamd daemon runs in the test environment - tests that exercise the
# malware-rejection path (services.malware_scan) mock it explicitly; every
# other upload test should hit the "clean" no-op path instead of a 503 from
# an unreachable scanner (see AppSettings.clamav_enabled's fail-closed
# behavior).
_app_settings.clamav_enabled = False

# model_bakery's default related-object generation collides with the
# create_user_profile post_save signal (see urbanlens.core.tests.baker).
BAKER_CUSTOM_CLASS = "urbanlens.core.tests.baker.SignalSafeBaker"

# model_bakery dispatches by exact field class, so EncryptedTextField (a
# TextField subclass used by ImmichAccount/FlickrAccount/GooglePhotosAccount/
# GoogleCalendarAccount/SiteSettings) isn't picked up by TextField's built-in
# generator - baker.make() would otherwise raise TypeError for any of those
# fields left at their default. Reuse the same plain-text generator TextField
# gets.
BAKER_CUSTOM_FIELDS_GEN = {
    "urbanlens.dashboard.models.fields.EncryptedTextField": "model_bakery.random_gen.gen_string",
}

globals().update(local_windows_gdal_overrides())
