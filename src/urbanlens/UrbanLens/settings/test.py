import atexit
import os
import shutil
import tempfile

from pydantic_core import Url

from urbanlens.UrbanLens.settings import _metrics
from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.app import settings as _app_settings
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

TESTING = True

# A `test` module is under test by definition; xdist workers hide pytest from argv, so fix storage here.
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405

# Query fingerprints land beside tests as reviewable diffs; CI fails on missing records instead.
PERF_REC = {"MODE": "none" if os.getenv("CI") else "once"}

# Production hashing is pure overhead in tests and can blow handshake timeouts.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# locmem: no live service needed and no cross-test bleed (network guard only allows localhost).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "urbanlens-tests",
    },
}

# In-process broker; tasks still don't run without ALWAYS_EAGER, so scheduling-only assertions hold.
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# One in-memory layer for the suite; flush in teardown if a lingering subscription ever bites.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# No clamd in tests; malware-path tests mock it, the rest take the clean no-op.
_app_settings.clamav_enabled = False

# Force None so a developer .env can't trigger live VirusTotal calls.
_app_settings.virustotal_api_key = None

# Guarantee tests never spend provider tokens, regardless of runner or local .env.
# Placeholder (not None) so adapter construction still exercises the tested path.
_app_settings.anthropic_api_key = "test-placeholder-not-a-key"
_app_settings.openai_api_key = "test-placeholder-not-a-key"
_app_settings.cloudflare_ai_api_key = "test-placeholder-not-a-key"
_app_settings.huggingface_ai_api_key = None
# Policy-valid host with no real account; keeps test artifacts shippable. Url-typed to match production.
_app_settings.cloudflare_worker_ai_endpoint = Url("https://api.cloudflare.com/client/v4/accounts/TESTACCOUNT/ai/run")
# Never address a real ai-inference service; tests mock at the adapter.
_app_settings.ai_inference_url = None

# Throwaway MEDIA_ROOT per process (TestCase rolls back the DB, not the filesystem).
_test_media_root = tempfile.mkdtemp(prefix="urbanlens-test-media-")
atexit.register(lambda: shutil.rmtree(_test_media_root, ignore_errors=True))
MEDIA_ROOT = _test_media_root

# Suite calls parsers directly; boundary tests re-raise to deny.
UL_UNTRUSTED_PARSE_POLICY = "allow"
# No media-worker drains queues under pytest; default routing plus eager still runs where asked.
UL_SANDBOX_ENABLED = False

# Suite calls LocalInferenceClient directly with mocked adapters.
UL_DIRECT_INFERENCE_POLICY = "allow"

# Assistant turns run in-process under eager; boundary tests flip this off.
UL_AI_WORKER_ENABLED = True

# Pin metrics off for determinism; derived values undone explicitly due to import order.
UL_METRICS_ENABLED = False
UL_METRICS_INSTRUMENTED = False
_app_settings.metrics_enabled = False
CELERY_WORKER_SEND_TASK_EVENTS = False
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django_prometheus"]  # noqa: F405
MIDDLEWARE = [middleware for middleware in MIDDLEWARE if not middleware.startswith("django_prometheus.")]  # noqa: F405
# Single-process values so per-test registries actually isolate (see helper).
_metrics.disable_multiprocess_metrics()

# model_bakery collides with the create_user_profile signal; use the signal-safe baker.
BAKER_CUSTOM_CLASS = "urbanlens.core.tests.baker.SignalSafeBaker"

# EncryptedTextField needs TextField's plain-text generator or baker.make() raises TypeError.
BAKER_CUSTOM_FIELDS_GEN = {
    "urbanlens.dashboard.models.fields.EncryptedTextField": "model_bakery.random_gen.gen_string",
}

globals().update(local_windows_gdal_overrides())
