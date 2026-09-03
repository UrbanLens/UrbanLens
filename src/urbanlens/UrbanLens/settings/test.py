import os

from urbanlens.UrbanLens.settings._gdal_windows import local_windows_gdal_overrides
from urbanlens.UrbanLens.settings.app import settings as _app_settings
from urbanlens.UrbanLens.settings.base import *  # noqa: F403

TESTING = True

# django-perf-rec writes each covered view's query *fingerprint* to a .perf.yml
# beside its test, so an N+1 arrives as a reviewable diff rather than as a
# number nobody can interpret.
#
# MODE decides what a missing record means. "once" writes it and passes, which
# is what you want the first time you cover a view. In CI a missing record means
# the file was never committed, and silently recording it there would assert
# whatever the code does today - including the regression under review - so it
# fails instead.
PERF_REC = {"MODE": "none" if os.getenv("CI") else "once"}

# Django's default PBKDF2 hasher runs ~1.2M iterations per call, which is the
# point in production and pure overhead in tests - every baked User, every
# generate_api_key, and every authenticate_api_key pays it. It was not merely
# slow: ApiKeyWebSocketAuthTests.test_valid_api_key_authenticates_an_anonymous_socket
# hashed inside the connection handshake and blew past WebsocketCommunicator's
# 1-second default connect timeout, failing as an opaque asyncio TimeoutError.
# (Its OAuth2 sibling passed throughout - that path is a plain indexed lookup
# with no hashing, which is what made the failure look consumer-specific.)
# Test-only: base.py keeps the real hashers for every other environment.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The real cache is Redis/valkey-backed, which makes every test whose request path
# touches the cache depend on a live external service. Two problems with that: the
# suite's own network guard (core.testing_network) only permits localhost, so running
# against a compose stack - where the cache resolves to a container bridge IP - fails
# any such test with an opaque "External network access is disabled during tests"
# rather than anything about the code; and tests would otherwise share one cache
# instance, so entries bleed between them. locmem is per-process and needs nothing
# running.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "urbanlens-tests",
    },
}

# Same reasoning as CACHES above, for the Celery broker. base.py points it at
# valkey, so every `apply_async` in a test opened a real broker connection - which
# the network guard blocks, raising RuntimeError, which `safely_enqueue_task`
# catches and reports as "broker unreachable" by returning None. Callers that treat
# that as "give up quietly" then took their failure path: the pin-detail panel views
# returned 204 instead of a panel, and twelve tests failed asserting a behaviour the
# code only exhibits when the broker is down.
#
# `memory://` is Celery's in-process transport - enqueueing succeeds and needs
# nothing running. Tasks still do not execute, since no worker consumes the queue
# and CELERY_TASK_ALWAYS_EAGER stays opt-in via UL_CELERY_TASK_ALWAYS_EAGER, so a
# test asserting a request only *scheduled* work still sees exactly that.
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# No clamd daemon runs in the test environment - tests that exercise the
# malware-rejection path (services.security.malware_scan) mock it explicitly; every
# other upload test should hit the "clean" no-op path instead of a 503 from
# an unreachable scanner (see AppSettings.clamav_enabled's fail-closed
# behavior).
_app_settings.clamav_enabled = False

# Forced to None (rather than relying on the field's own default) so a real
# UL_VIRUSTOTAL_API_KEY in a developer's local .env can't make the suite make
# live network calls. Tests that exercise the VirusTotal path patch this back
# explicitly - see services.security.virustotal_scan.
_app_settings.virustotal_api_key = None

# The suite calls the parsers directly - that is how they are unit tested - so
# the sandbox boundary is not enforced here. The tests that verify the boundary
# itself (services/sandbox) raise it back to "deny" with override_settings.
UL_UNTRUSTED_PARSE_POLICY = "allow"
# No media-worker container drains a queue under pytest; with this off, the
# untrusted-parse tasks keep their default routing and CELERY_TASK_ALWAYS_EAGER
# still runs them in-process where a test asks for it.
UL_SANDBOX_ENABLED = False

# Same reasoning as UL_UNTRUSTED_PARSE_POLICY above: the suite calls
# LocalInferenceClient directly (mocking the provider adapters, never a real
# provider) - the tests that verify this boundary itself raise it back to
# "deny" with override_settings.
UL_DIRECT_INFERENCE_POLICY = "allow"

# No ai-worker container drains Queue.AI under pytest either, but the suite
# exercises assistant availability directly (CELERY_TASK_ALWAYS_EAGER runs the
# turn task in-process where a test asks for it) - the tests that verify this
# boundary itself set UL_AI_WORKER_ENABLED = False with override_settings.
UL_AI_WORKER_ENABLED = True

# Pinned off so the suite is deterministic whatever the developer's .env says.
# It is read at import time to decide whether /metrics is routed and whether the
# django-prometheus middleware is installed, so a machine with metrics enabled
# would otherwise run the suite against a different URLconf and middleware stack
# than CI does. The tests that need it on flip it explicitly.
UL_METRICS_ENABLED = False

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
