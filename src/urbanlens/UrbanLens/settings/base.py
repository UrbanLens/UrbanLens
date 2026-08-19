from __future__ import annotations

import os

from celery.schedules import crontab
from pathlib import Path
import sys

from django.core.management.utils import get_random_secret_key
from dotenv import find_dotenv, load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env - search upward from this file so the
# repo-root .env is found regardless of working directory.
load_dotenv(find_dotenv())

# Detect the current environment early - other settings branch on it.
ENVIRONMENT_NAME = os.getenv("UL_ENVIRONMENT", "local").lower()
_is_local = ENVIRONMENT_NAME == "local"
_is_dev = ENVIRONMENT_NAME in {"local", "development"}

# SECURITY WARNING: keep the secret key used in production secret!
#
# The random fallback is a data-loss hazard wherever encrypted data can exist,
# not just a session-stability one: SECRET_KEY is also the fallback source for
# EncryptedTextField's key (dashboard/models/fields.py), gunicorn runs without
# preload_app, and celery/manage.py are separate processes - so an unset value
# gives every process a different key, and anything written to an encrypted
# field is unreadable by every other process and after the next restart. Fail
# loudly instead of degrading, anywhere a real database is in play.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ""
if not SECRET_KEY:
    # Ephemeral keys are only safe where no durable encrypted data exists:
    # developer machines and test runs. staging/production must fail.
    _key_optional = _is_dev or ENVIRONMENT_NAME == "testing" or any(arg.endswith("pytest") or "pytest" in arg for arg in sys.argv)
    if not _key_optional:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            f"DJANGO_SECRET_KEY must be set when UL_ENVIRONMENT is '{ENVIRONMENT_NAME}'. "
            "Without it every process derives its own random key, which breaks sessions "
            "across workers and permanently orphans anything already written to an "
            "encrypted field. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(64))" '
            "- see .env-sample and docs/DATA_ENCRYPTION.md.",
        )
    SECRET_KEY = get_random_secret_key()


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"true", "1", "yes"}


# Test clients issue HTTP requests. Django's DiscoverRunner disables HTTPS
# redirects in setup_test_environment(), but pytest-django imports settings
# directly and does not run that project test runner hook.
TESTING = _env_bool("DJANGO_TESTING", False) or any(
    arg.endswith("pytest") or "pytest" in arg for arg in sys.argv
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DJANGO_DEBUG", _is_dev)

# ALLOWED_HOSTS: AppSettings is the source of truth (override via UL_ALLOWED_HOSTS,
# a comma-separated list). Local environment defaults to wildcard-friendly hosts so
# developers can access the site immediately without any configuration.
from urbanlens.UrbanLens.settings._env import env_bool  # noqa: E402
from urbanlens.UrbanLens.settings.app import settings as _app_settings  # noqa: E402

ALLOWED_HOSTS = _app_settings.allowed_hosts

# Application definition
INSTALLED_APPS = [
    # "daphne" must come before "django.contrib.staticfiles" - Channels patches
    # the `runserver` management command to be ASGI/WebSocket-aware only when
    # daphne is registered ahead of it, which is what gives local dev working
    # WebSockets with no extra process (production instead runs a dedicated
    # daphne container - see docker-compose.yml's `app-ws` service).
    "daphne",
    "channels",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django.contrib.humanize",
    "corsheaders",
    # Registers django-csp's system checks (the app itself defines no models);
    # csp.E001 fires if anyone reintroduces the pre-4.0 `CSP_*` setting format,
    # which django-csp 4 silently ignores.
    "csp",
    "urbanlens.dashboard.apps.DashboardConfig",
    "social_django",
    # OAuth2/OIDC provider for native clients (mobile/desktop apps) hitting the
    # external API - browser sessions and PAT-style ApiKeys are unaffected.
    "oauth2_provider",
    # OpenAPI schema generation, served only for the external API surface
    # (see external_api.schema's preprocessing hook).
    "drf_spectacular",
]

# Routes the websocket protocol (see UrbanLens/asgi.py); HTTP keeps using
# WSGI_APPLICATION in production (gunicorn) - only the dedicated `app-ws`
# daphne container and local `runserver` actually serve ASGI traffic.
ASGI_APPLICATION = "urbanlens.UrbanLens.asgi.application"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Emits the Content-Security-Policy header built from CONTENT_SECURITY_POLICY
    # (or ..._REPORT_ONLY) below. Sits directly under SecurityMiddleware so the
    # header is attached to every response that leaves the stack, including ones
    # short-circuited further in.
    "csp.middleware.CSPMiddleware",
    # CorsMiddleware must sit above CommonMiddleware (and anything else that
    # can short-circuit a response) so CORS headers are applied to redirects
    # and preflight responses - see django-cors-headers docs.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Innermost: swaps in the simulated viewer for "view profile as" previews.
    "urbanlens.dashboard.middleware.ProfilePreviewMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "social_core.backends.google.GoogleOAuth2",
    "social_core.backends.discord.DiscordOAuth2",
    "urbanlens.dashboard.services.auth.auth_backend.EmailOrUsernameModelBackend",
]

ROOT_URLCONF = "urbanlens.UrbanLens.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Explicit DIRS is searched before the APP_DIRS loader, so this app's
        # own registration/*.html and password_reset_*.txt templates always
        # win over django.contrib.admin/auth's bundled templates of the same
        # name (both apps are registered ahead of "dashboard" in
        # INSTALLED_APPS, so without this the app_directories loader picks
        # theirs first and ours is silently never rendered - UL-257).
        "DIRS": [os.path.join(PROJECT_ROOT, "dashboard", "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "urbanlens.dashboard.context_processors.add_page_name",
                "urbanlens.dashboard.context_processors.add_site_settings",
                "urbanlens.dashboard.context_processors.add_dev_toolbar",
                "urbanlens.dashboard.context_processors.add_feature_access",
                "urbanlens.dashboard.context_processors.add_pending_account_deletion",
                "urbanlens.dashboard.context_processors.add_environment_indicator",
                "urbanlens.dashboard.context_processors.add_distance_units",
                "urbanlens.dashboard.context_processors.add_direct_messages",
            ],
        },
    },
]

WSGI_APPLICATION = "urbanlens.UrbanLens.wsgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": os.getenv("UL_DB_ENGINE", "django.contrib.gis.db.backends.postgis"),
        "NAME": os.getenv("UL_DB_NAME", "urbanlens"),
        "USER": os.getenv("UL_DB_USER", "urbanlens"),
        "PASSWORD": os.getenv("UL_DB_PASS"),
        "HOST": os.getenv("UL_DB_HOST", "localhost"),
        "PORT": os.getenv("UL_DB_PORT", "5432"),
        # UL_TEST_DB_NAME lets concurrent test runs (e.g. two working copies
        # or agent sessions on one machine) use separate test databases
        # instead of fighting over the default "test_<NAME>".
        "TEST": {"NAME": os.getenv("UL_TEST_DB_NAME") or None},
    },
}
# Valkey/Redis cache. Used for per-profile map pin payloads and Django's
# transient application cache when UL_VALKEY_URL/UL_REDIS_URL is configured.
VALKEY_URL = os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
if VALKEY_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": VALKEY_URL,
            "KEY_PREFIX": "urbanlens",
            "VERSION": 1,
            "TIMEOUT": 300,
            "OPTIONS": {
                "max_connections": 50,
                "socket_connect_timeout": 1,
                "socket_timeout": 2,
                "retry_on_timeout": True,
            },
        },
    }
    # Cache-backed sessions avoid per-request DB reads on every page load.
    # cached_db writes through to the database so sessions survive a cache flush.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"

    # Django Channels layer backed by Valkey for cross-process group messaging.
    #
    # socket_timeout MUST be comfortably larger than RedisChannelLayer.brpop_timeout
    # (5s, hardcoded upstream). redis-py's default socket_timeout is also 5s, so
    # with no override here every long-poll BRPOP raced its own read timeout -
    # any latency jitter (GC pause, a busy Valkey tick) pushed the read past
    # 5.000s and raised redis.exceptions.TimeoutError, even with a healthy
    # server. Because channels_redis serializes all receive() calls in a
    # process behind one asyncio.Lock, that single race repeating tore down
    # every websocket in this process, not just the one that timed out.
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [
                    {
                        "address": VALKEY_URL,
                        "socket_connect_timeout": 5,
                        "socket_timeout": 20,
                        "retry_on_timeout": True,
                        "health_check_interval": 30,
                    },
                ],
                "capacity": 1500,
                "expiry": 60,
                # Channel-group names are derived from model pks
                # (``profile_notifications_<id>``), and every test database
                # restarts its sequences at 1 - so two concurrent test runs
                # produce identical group names. UL_TEST_DB_NAME isolates
                # Postgres but not this layer, which left websocket tests in
                # one run receiving (or losing) another run's messages: a
                # flake that only ever appeared when suites overlapped. The
                # per-run prefix closes that; outside tests it is the
                # channels_redis default.
                **({"prefix": f"asgi-test-{os.getenv('UL_TEST_DB_NAME', 'default')}"} if TESTING else {}),
            },
        },
    }

DATABASE_ROUTERS = ["urbanlens.dashboard.dbrouters.DBRouter"]

# Celery - background job processing. Defaults to the configured Valkey/Redis
# endpoint when available, otherwise local Redis for development.
CELERY_BROKER_URL = os.getenv("UL_CELERY_BROKER_URL") or VALKEY_URL or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = os.getenv("UL_CELERY_RESULT_BACKEND") or CELERY_BROKER_URL
# Bounds the result backend's connection-recovery retry loop
# (RedisBackend.ensure(), used by its pub/sub result-tracking reconnect) to a
# few seconds instead of Celery's default near-unbounded exponential backoff
# (interval_start=2s, interval_max=30s, no timeout/max_retries set) - without
# this, any request-path call to safely_enqueue_task() blocks for several
# minutes before failing whenever the broker is unreachable (it does catch
# the eventual RuntimeError, but only after the retry storm completes).
# Matches the fail-fast philosophy already applied to the plain Django
# cache's own Redis connection above (socket_connect_timeout/socket_timeout).
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {"retry_policy": {"timeout": 5.0}}
# With a Redis broker and CELERY_TASK_ACKS_LATE, any message unacked past
# visibility_timeout is redelivered to another worker - Redis's default is
# 3600s, which exactly equals both the hard CELERY_TASK_TIME_LIMIT above and
# the longest countdown= this app schedules (import/export cleanup at 3600s,
# check-in archival at ARCHIVE_VIEWER_GRACE_PERIOD = 1h). At that boundary a
# legitimately long task, or a countdown sitting in a worker, is duplicated
# right as it finishes/fires. Keep this comfortably above
# max(time_limit, longest countdown); raise it if either grows.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 2 * 60 * 60}
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = os.getenv("UL_CELERY_TIMEZONE", "UTC")
CELERY_TASK_ALWAYS_EAGER = os.getenv("UL_CELERY_TASK_ALWAYS_EAGER", "False").lower() in {"true", "1", "yes"}
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("UL_CELERY_TASK_SOFT_TIME_LIMIT", "2700"))
CELERY_TASK_TIME_LIMIT = int(os.getenv("UL_CELERY_TASK_TIME_LIMIT", "3600"))
# Backup defaults. Site admins can override these values in the database-backed settings UI.
UL_BACKUP_ENABLED = os.getenv("UL_BACKUP_ENABLED", "True").lower() in {"true", "1", "yes"}
UL_BACKUP_FREQUENCY_HOURS = int(os.getenv("UL_BACKUP_FREQUENCY_HOURS", "24"))
UL_BACKUP_RETENTION = int(os.getenv("UL_BACKUP_RETENTION", "30"))

# Leaflet zoom level at/above which a saved MarkupMap viewport is considered
# "zoomed in" for pin-share detection purposes (see
# services.sharing.map_pin_share_detection.is_zoomed_in): every one of the sender's
# pins visible in frame counts as shared, regardless of markup content. Below
# this, only pins specifically called out by markup (in-boundary marker,
# arrow pointing toward, or shape overlap) count.
UL_MAP_SHARE_ZOOM_THRESHOLD = float(os.getenv("UL_MAP_SHARE_ZOOM_THRESHOLD", "14"))

# Interval schedules all fire relative to beat start, so same-interval entries
# fire *simultaneously* - eleven hourly sweeps would stampede the default queue
# at once each hour, delaying user-facing tasks (image processing shares it).
# Hourly work is therefore staggered across distinct crontab minutes and daily
# work across off-peak UTC hours. The 5-minute safety-check-in chain
# stays interval-based on purpose: it is time-critical, cheap, and its four
# tasks are sequenced by their own due-time filters rather than by spacing.
CELERY_BEAT_SCHEDULE = {
    "scheduled-database-backup-check": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_database_backup",
        "schedule": crontab(minute=2),
    },
    "scheduled-vestigial-asset-cleanup": {
        "task": "urbanlens.dashboard.tasks.cleanup_vestigial_assets_task",
        "schedule": crontab(minute=7),
    },
    "scheduled-location-enrichment": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_enrichment",
        "schedule": crontab(minute=12),
    },
    "scheduled-trivia-generation": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_trivia_generation",
        "schedule": crontab(minute=17),
    },
    "scheduled-trivia-wiki-incorporation": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_trivia_wiki_incorporation",
        "schedule": crontab(minute=22),
    },
    "spotguessr-stall-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_stalled_spotguessr_sessions",
        "schedule": 2 * 60,
    },
    "trivia-stall-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_stalled_trivia_sessions",
        "schedule": 2 * 60,
    },
    "consensus-stall-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_stalled_consensus_sessions",
        "schedule": 2 * 60,
    },
    # Catches thresholds no write crosses - "trips attended" ticks up simply
    # because a trip's end date passed - and anything a signal enqueue lost.
    "achievements-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_achievements",
        "schedule": crontab(hour=3, minute=10),
    },
    # Safety net for missed Stripe webhook deliveries - the core "did this
    # charge clear the threshold" mechanic runs at webhook time, not here.
    "stripe-subscriptions-sync": {
        "task": "urbanlens.dashboard.tasks.sync_stripe_subscriptions",
        "schedule": crontab(hour=4, minute=10),
    },
    # Keeps a canceled pay-what-you-want subscription's banked-access balance counting
    # down over time - invoice.payment_succeeded is the only other trigger, and it stops
    # firing entirely once Stripe considers the subscription gone.
    "pwyw-usage-ledger-sweep": {
        "task": "urbanlens.dashboard.tasks.advance_pwyw_usage_ledgers",
        "schedule": crontab(hour=4, minute=40),
    },
    "safety-checkin-due-reminders": {
        "task": "urbanlens.dashboard.tasks.send_due_checkin_reminders",
        "schedule": 5 * 60,
    },
    "safety-checkin-final-warnings": {
        "task": "urbanlens.dashboard.tasks.send_final_checkin_warnings",
        "schedule": 5 * 60,
    },
    "safety-checkin-escalation": {
        "task": "urbanlens.dashboard.tasks.escalate_overdue_checkins",
        "schedule": 5 * 60,
    },
    "safety-checkin-archival-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_due_safety_checkin_archival",
        "schedule": 5 * 60,
    },
    "account-deletion-reminders": {
        "task": "urbanlens.dashboard.tasks.send_account_deletion_reminders",
        "schedule": crontab(minute=27),
    },
    "account-deletion-hard-delete": {
        "task": "urbanlens.dashboard.tasks.hard_delete_expired_accounts",
        "schedule": crontab(minute=32),
    },
    "safety-checkin-auto-delete": {
        "task": "urbanlens.dashboard.tasks.delete_expired_safety_checkins",
        "schedule": crontab(minute=37),
    },
    "undo-action-pruning": {
        "task": "urbanlens.dashboard.tasks.prune_expired_undo_actions",
        "schedule": crontab(minute=42),
    },
    "direct-message-hard-delete": {
        "task": "urbanlens.dashboard.tasks.hard_delete_expired_direct_messages",
        "schedule": crontab(minute=47),
    },
    "upgrade-placeholder-pin-names": {
        "task": "urbanlens.dashboard.tasks.upgrade_placeholder_pin_names",
        "schedule": crontab(minute=52),
    },
    # Daily is plenty: retention is measured in hundreds of days
    # (services.pins.pin_sync.TOMBSTONE_RETENTION), and the pins/deleted/ feed's 410
    # full-resync signal guards clients against any pruning-induced gap.
    "pin-tombstone-pruning": {
        "task": "urbanlens.dashboard.tasks.prune_pin_tombstones",
        "schedule": crontab(hour=5, minute=10),
    },
    # Daily. Retention (400 days) is set by the costs page's 12-month spend
    # chart, the longest reader of this table - see prune_api_call_logs.
    "api-call-log-pruning": {
        "task": "urbanlens.dashboard.tasks.prune_api_call_logs",
        "schedule": crontab(hour=5, minute=40),
    },
    "public-pin-candidate-evaluation": {
        "task": "urbanlens.dashboard.tasks.evaluate_public_pin_candidates",
        "schedule": crontab(minute=57),
    },
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {
            "user_attributes": ("username", "email", "first_name", "last_name"),
            "max_similarity": 0.5,  # stricter than default 0.7
        },
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
    {
        "NAME": "urbanlens.dashboard.validators.password.ComplexityValidator",
    },
    {
        "NAME": "urbanlens.dashboard.validators.password.HaveIBeenPwnedValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(PROJECT_ROOT, "frontend", "static")
STATICFILES_DIRS = [
    os.path.join(PROJECT_ROOT, "dashboard/frontend/static"),
]
# CompressedManifestStaticFilesStorage requires collectstatic to have been run
# to generate the manifest; the test suite never runs collectstatic, so fall
# back to plain (non-hashed) storage there.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if TESTING else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(PROJECT_ROOT, "media")

# Authenticated media serving (dashboard.controllers.media.MediaGateView).
# When nginx fronts the app (docker/staging/production), the gate view answers
# authorized requests with an X-Accel-Redirect to the internal-only
# /_protected_media/ location and nginx streams the file; in local dev
# (runserver, no nginx) the view streams the file itself via FileResponse.
# Override with UL_MEDIA_X_ACCEL if a deployment diverges from this default
# (e.g. a development-flagged docker stack that still runs behind nginx and
# wants the more efficient handoff).
MEDIA_X_ACCEL = _env_bool("UL_MEDIA_X_ACCEL", not _is_dev)
# Must match the `location /_protected_media/` block in
# src/urbanlens/config/nginx/django.conf.
MEDIA_X_ACCEL_PREFIX = "/_protected_media/"

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Reject plain HTTP in production. Local and development environments allow it
# by default so developers can access the site without TLS configuration.
# Override via UL_UNSAFE_ALLOW_HTTP in .env (or set to False to enforce HTTPS locally).
_http_default = "True" if _is_dev else "False"
UNSAFE_ALLOW_HTTP = _env_bool("UL_UNSAFE_ALLOW_HTTP", _http_default == "True")
SECURE_SSL_REDIRECT = not UNSAFE_ALLOW_HTTP and not TESTING
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", SECURE_SSL_REDIRECT)
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", SECURE_SSL_REDIRECT)
# Internal container health checks hit /health over HTTP on the app port.
SECURE_REDIRECT_EXEMPT = [r"^health"]

# HSTS, gated on exactly the same condition as SECURE_SSL_REDIRECT so an
# intentionally HTTP-only deployment (UL_UNSAFE_ALLOW_HTTP, the dev default) and
# the test suite are unaffected. Without it, SECURE_SSL_REDIRECT alone still
# leaves a first visit strippable: that redirect is itself served over HTTP, so
# an attacker on the path can answer it instead. A year, with subdomains, is the
# usual production value; preload is deliberately left off, since submitting a
# domain to the preload list is a decision for whoever owns it - it is painful to
# reverse and this project is self-hosted by design.
SECURE_HSTS_SECONDS = int(os.getenv("UL_HSTS_SECONDS", "31536000")) if SECURE_SSL_REDIRECT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("UL_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_SECONDS > 0)

# Content-Security-Policy (django-csp >= 4, which takes the CONTENT_SECURITY_POLICY
# dict rather than the pre-4.0 flat `CSP_*` settings - those are silently ignored,
# and csp.E001 flags them if they reappear).
#
# Every host below was read out of a template or a frontend module rather than
# assumed; the inventory, and which file each host came from, is in docs/NOTES.md.
# Leaflet expands the `{s}` placeholder in its tile templates to a/b/c subdomains,
# so the tile hosts need both the wildcard and the bare form.
#
# 'unsafe-inline' in script-src is load-bearing, not laziness: the frontend has
# ~99 inline <script> blocks (starting with the anti-FOUC block in themes/base.html),
# HTMX `hx-on:` attributes, and json_script payloads. Removing it requires threading
# a nonce through every one of those - tracked as the inline-JS extraction roadmap
# item. Until that lands, script-src buys host restriction (an injected
# `<script src=//evil>` is blocked) but not injected-inline-script protection.
# Note also that a nonce and 'unsafe-inline' cannot coexist: browsers ignore
# 'unsafe-inline' as soon as a nonce is present, so the migration has to convert
# every inline block at once per response, not incrementally.
_CSP_DIRECTIVES: dict[str, object] = {
    "default-src": ["'self'"],
    # jQuery/toastr/Leaflet/HTMX/Chart.js/Sortable are all loaded from CDNs by
    # themes/base.html and the per-page templates; maps.googleapis.com is injected
    # at runtime by the SpotGuessr Street View round.
    "script-src": [
        "'self'",
        "'unsafe-inline'",
        "https://code.jquery.com",
        "https://cdnjs.cloudflare.com",
        "https://unpkg.com",
        "https://cdn.jsdelivr.net",
        "https://maps.googleapis.com",
    ],
    # 'unsafe-inline' here covers the inline style="" attributes used throughout
    # the templates as well as Leaflet's runtime positioning styles.
    "style-src": [
        "'self'",
        "'unsafe-inline'",
        "https://fonts.googleapis.com",
        "https://cdnjs.cloudflare.com",
        "https://unpkg.com",
    ],
    "font-src": ["'self'", "data:", "https://fonts.gstatic.com", "https://cdnjs.cloudflare.com"],
    "img-src": [
        "'self'",
        "data:",
        "blob:",
        # Any HTTPS image host, because map image overlays are a paste-any-URL
        # feature (_map_overlays_list.html, map-image-overlays.ts) - a finite
        # list would make every overlay outside it vanish the moment an
        # operator sets UL_CSP_ENFORCE. Widening img-src is the cheap half of
        # the trade: images do not execute, and the alternative is proxying
        # arbitrary user-supplied URLs through the server, which buys an SSRF
        # surface to avoid a directive that never blocked script. The named
        # hosts below stay for the documentation value.
        "https:",
        # Base map tiles and overlays (frontend/ts/shared/map-layers.ts).
        "https://*.tile.openstreetmap.org",
        "https://tile.openstreetmap.org",
        "https://*.basemaps.cartocdn.com",
        "https://basemaps.cartocdn.com",
        "https://*.tile.opentopomap.org",
        "https://tile.opentopomap.org",
        "https://server.arcgisonline.com",
        "https://services.arcgisonline.com",
        "https://tile.openweathermap.org",
        # Leaflet's default marker/shadow PNGs (frontend/ts/entries/map-annotations.ts).
        "https://cdnjs.cloudflare.com",
        # Result favicons on the web-search page and the Gravatar avatar preview.
        "https://www.google.com",
        "https://www.gravatar.com",
        # Google Maps JS API imagery, including Street View panorama tiles. The
        # API picks its own image hosts at runtime, so this list is the known set
        # rather than a proven-complete one - report-only mode is what will show
        # whether anything else is needed.
        "https://maps.googleapis.com",
        "https://maps.gstatic.com",
    ],
    # ws: alongside wss: because local/dev deployments are served over plain HTTP
    # (UNSAFE_ALLOW_HTTP) and the game sockets follow the page protocol.
    "connect-src": [
        "'self'",
        "ws:",
        "wss:",
        # Browser-side geocoding, deliberately unproxied (location-search-engine.ts).
        "https://nominatim.openstreetmap.org",
        # Place summaries fetched inline by the map page.
        "https://en.wikipedia.org",
        "https://maps.googleapis.com",
    ],
    # The Street View embed on the location page.
    "frame-src": ["'self'", "https://www.google.com"],
    "media-src": ["'self'", "data:", "blob:"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    # NOTE: X_FRAME_OPTIONS is "DENY", which is stricter than this. Browsers that
    # honour frame-ancestors ignore X-Frame-Options entirely, so enforcing this
    # policy relaxes framing from "nobody" to "same origin only". Change this to
    # 'none' if the DENY posture is meant to be kept.
    "frame-ancestors": ["'self'"],
    "form-action": ["'self'"],
}

# Report-only by default: the header is emitted and violations are reported, but
# nothing is blocked, so a policy mistake shows up in reports instead of as a
# broken page. Flip per environment with UL_CSP_ENFORCE=true once that
# environment's reports are clean. Exactly one of the two settings is defined -
# django-csp emits a header for each one that exists.
CSP_ENFORCE = _app_settings.csp_enforce
if CSP_ENFORCE:
    CONTENT_SECURITY_POLICY = {"DIRECTIVES": _CSP_DIRECTIVES}
else:
    CONTENT_SECURITY_POLICY_REPORT_ONLY = {"DIRECTIVES": _CSP_DIRECTIVES}

# Trust the X-Forwarded-Proto header set by Nginx so Django builds https:// URLs
# when sitting behind a reverse proxy that terminates SSL.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Proxy hops whose X-Forwarded-For entries are ours rather than the client's.
# Read by the per-IP rate limiters; see the field description in settings/app.py.
TRUSTED_PROXY_COUNT = _app_settings.trusted_proxy_count

protocols = ["https://"]
if _is_local:
    # Local development: cover common ports used by docker-compose and direct runserver.
    domains = [
        "urbanlens.org",
        "localhost",
        "localhost:8000",
        "localhost:21080",
        "localhost:21800",
        "127.0.0.1",
        "127.0.0.1:8000",
        "127.0.0.1:21080",
        "127.0.0.1:21800",
        "[::1]",
        "[::1]:8000",
    ]
elif _is_dev:
    domains = ["urbanlens.org", "localhost", "localhost:21080", "localhost:21800", "127.0.0.1"]
else:
    domains = ["urbanlens.org", "localhost", "localhost:21080"]

subdomains = ["www.", ""]
if UNSAFE_ALLOW_HTTP:
    protocols.append("http://")

CORS_ALLOWED_ORIGINS = list(dict.fromkeys(
    f"{protocol}{subdomain}{domain}"
    for protocol in protocols
    for subdomain in subdomains
    for domain in domains
    if not (subdomain and domain.startswith("["))  # IPv6 literals can't have a subdomain prefix
))
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS.copy()

SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.getenv("UL_GOOGLE_CLIENT_ID", "")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.getenv("UL_GOOGLE_CLIENT_SECRET", "")
SOCIAL_AUTH_DISCORD_KEY = os.getenv("UL_DISCORD_CLIENT_ID", "")
SOCIAL_AUTH_DISCORD_SECRET = os.getenv("UL_DISCORD_CLIENT_SECRET", "")
SOCIAL_AUTH_DISCORD_SCOPE = ["identify", "email"]

# Custom social-auth pipeline.
# Replaces get_username with provider handle when available, else random name.
# Fetches and saves the provider avatar (or Gravatar) after the user is created.
# Clears last_name on new accounts to limit personal data exposure.
SOCIAL_AUTH_PIPELINE = (
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "social_core.pipeline.social_auth.social_user",
    # Provider username when free, else random adjective+animal+number.
    "urbanlens.dashboard.services.social_auth.pipeline.generate_sso_username",
    "social_core.pipeline.user.create_user",
    "social_core.pipeline.social_auth.associate_user",
    "social_core.pipeline.social_auth.load_extra_data",
    # user_details copies first_name, last_name, email from provider.
    "social_core.pipeline.user.user_details",
    # Strip last_name to preserve partial anonymity for new accounts.
    "urbanlens.dashboard.services.social_auth.pipeline.suppress_last_name_for_new_users",
    # Download and store the provider avatar (or Gravatar) if none exists yet.
    "urbanlens.dashboard.services.social_auth.pipeline.fetch_and_save_avatar",
    # Flag new SSO users for onboarding (username + avatar selection).
    "urbanlens.dashboard.services.social_auth.pipeline.mark_new_user_onboarding",
    # Save Discord username as a social link for Discord SSO users.
    "urbanlens.dashboard.services.social_auth.pipeline.save_discord_social_link",
    # Must be last: detours through the 2FA challenge (instead of the implicit
    # login social-auth performs once the pipeline finishes) for any account
    # that already has a passkey or authenticator app enrolled.
    "urbanlens.dashboard.services.social_auth.pipeline.enforce_two_factor_for_sso",
)

# After login/signup, send users through post-login routing (map or site admin setup).
LOGIN_REDIRECT_URL = "/accounts/post-login/"
LOGIN_URL = "/accounts/login/"
LOGOUT_REDIRECT_URL = "/"

# social-auth redirects after OAuth completion
SOCIAL_AUTH_LOGIN_REDIRECT_URL = "/accounts/post-login/"
SOCIAL_AUTH_NEW_USER_REDIRECT_URL = "/accounts/post-login/"

# Email backend - use console in dev, configure via env in production
EMAIL_BACKEND = os.getenv("UL_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("UL_EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("UL_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("UL_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("UL_EMAIL_PASSWORD", "")
# Parsed leniently: app.py declares these same two variables as pydantic bools, which
# accept true/1/yes, so a literal == "True" here made the two readers of one variable
# disagree - and for TLS the disagreement resolved toward sending mail in plaintext.
EMAIL_USE_TLS = env_bool("UL_EMAIL_TLS", default=True)
EMAIL_USE_SSL = env_bool("UL_EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = os.getenv("UL_EMAIL_FROM", "noreply@yourdomain.org")
# Canonical base URL used to build absolute links in emails/notifications sent
# from contexts with no HttpRequest to build them from (e.g. Celery tasks).
_site_url_env = os.getenv("UL_SITE_URL")
SITE_URL = _site_url_env or "http://localhost:21080"
if not _site_url_env and not _is_dev:
    import logging

    logging.getLogger(__name__).warning(
        "UL_SITE_URL is not set outside a local/development environment - falling back to "
        "%r. Emails and safety alerts will contain broken links until UL_SITE_URL is set to "
        "this deployment's real public URL.",
        SITE_URL,
    )
SMITHSONIAN_API_KEY = os.getenv("UL_SMITHSONIAN_API_KEY", "")
GOOGLE_UNRESTRICTED_API_KEY = os.getenv("UL_GOOGLE_UNRESTRICTED_API_KEY", "")
GOOGLE_DOMAIN_RESTRICTED_API_KEY = os.getenv("UL_GOOGLE_DOMAIN_RESTRICTED_API_KEY", "")
GOOGLE_SEARCH_TENANT = os.getenv("UL_GOOGLE_SEARCH_CX") or os.getenv("UL_GOOGLE_SEARCH_TENANT", "")
OPEN_WEATHER_API_KEY = os.getenv("UL_OPENWEATHERMAP_API_KEY", "")
NPS_API_KEY = os.getenv("UL_NPS_API_KEY", "")

TEST_RUNNER = "urbanlens.core.tests.runner.TestRunner"

# DRF global throttle limits - authenticated users get generous burst/day limits;
# anonymous requests (e.g. public API endpoints) are tightly constrained.
# Requires Valkey cache to be configured - no-ops gracefully when cache is absent.
REST_FRAMEWORK = {
    # Every registered API endpoint is user-scoped; opt out per-view if a
    # genuinely public endpoint is ever added.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "600/minute",
        # external_api.throttling - per credential, not per user, so one
        # connected app's misbehavior can't burn through a budget shared with a
        # user's other keys. Split by tier because an interactive sync client
        # reads far more than it writes: a flat cap generous enough for a full
        # resync would also be generous enough for a runaway write loop.
        # The burst cap applies on top of both tiers, bounding a stampede
        # without lowering the hourly ceiling.
        "external_api_read": "1000/hour",
        "external_api_write": "300/hour",
        "external_api_burst": "60/minute",
        # Credential-authenticated /media/ fetches (controllers.media). One
        # gallery screen is dozens of files, so this is deliberately far more
        # generous than the burst cap - but it is still a cap, so a leaked key
        # cannot be used as an unmetered CDN.
        "external_api_media": "2000/hour",
        # Applied on top of the above, to the handful of endpoints whose cost
        # is unbounded in the caller's own data rather than fixed per request
        # (currently the smart-list resync). See
        # external_api.throttling.ExternalApiResyncThrottle.
        "external_api_resync": "12/hour",
        # Autocomplete replaces (rather than adds to) the read cap for the
        # location-search endpoints - it is charged per keystroke, so counting
        # it against the shared read budget would let a few minutes of typing
        # starve the client's actual syncing. Clients are still expected to
        # debounce; the burst cap above applies here too.
        "external_api_location_search": "1200/hour",
        # Starting a game session runs up to 25 eligibility passes over the
        # player's pins, an N+1 difficulty-proxy lookup across them, and a
        # *billed* Street View call per attempt - resync-shaped cost, so it gets
        # a resync-shaped cap rather than a share of the write budget. Generous
        # enough for dozens of real games an hour.
        # See external_api.throttling.GameStartThrottle.
        "external_api_game_start": "40/hour",
        # Global search fans one request out across every domain provider
        # (pins, wikis, trips, photos, messages, ...), so a single call is far
        # from a single query. Its own budget keeps a search-heavy session from
        # starving the client's actual syncing, and vice versa.
        "external_api_global_search": "300/hour",
        # Calendar export talks to Google on the request path and may make one
        # upstream call per trip activity. Tight, because the cost lands on a
        # third party's rate limit as much as on ours.
        "external_api_calendar": "30/hour",
        # One assistant chat turn bills real model-provider cost and can fan
        # out to up to 6 model round trips before it replies - resync-shaped
        # cost, same reasoning as external_api_game_start.
        "external_api_assistant_message": "60/hour",
        # Live-location updates while a check-in is active are a foreground-
        # tracking workload, not an ordinary write - the standard write cap
        # (300/hour) dies in under an hour at one fix per 10 seconds. Its own
        # budget accommodates that cadence without loosening the cap every
        # other safety write shares.
        "external_api_safety_location": "360/hour",
    },
    # Only consulted by views whose schema is actually generated - the
    # preprocessing hook in external_api.schema limits that to the external API.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI schema for the external API only - the internal HTMX/REST surface has
# no public contract and is deliberately excluded (external_api.schema).
SPECTACULAR_SETTINGS = {
    "TITLE": "UrbanLens External API",
    "DESCRIPTION": "Versioned API for external applications and native clients holding a user's API key or OAuth2 token.",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "PREPROCESSING_HOOKS": ["urbanlens.dashboard.external_api.schema.preprocess_external_api_only"],
    # Stable names for choice sets spectacular would otherwise hash
    # (Status0ebEnum, ...). Hashed names are derived from the *colliding set*,
    # so adding one more `status` field can renumber the rest and silently
    # break a generated client's types. Subset lists (a write serializer
    # restricting the settable states) are spelled out; full model choice
    # sets are referenced by import string so they follow the model.
    "ENUM_NAME_OVERRIDES": {
        "SafetyCheckinStatusEnum": "urbanlens.dashboard.models.safety.model.SafetyCheckinStatus.choices",
        "SafetyCheckinPartnerStatusEnum": "urbanlens.dashboard.models.safety.model.SafetyCheckinPartnerStatus.choices",
        "FriendshipStatusEnum": "urbanlens.dashboard.models.friendship.meta.FriendshipStatus.choices",
        "TripActivityStatusEnum": "urbanlens.dashboard.models.trips.model.TripActivity.STATUS_CHOICES",
        "TripActivitySettableStatusEnum": ["proposed", "confirmed"],
        "LabelKindEnum": "urbanlens.dashboard.models.labels.meta.KIND_CHOICES",
    },
}

# OAuth2 provider (django-oauth-toolkit) - the auth path for native clients
# (the mobile app registers as a *public* client and must use PKCE; PAT-style
# ApiKeys remain for simple server-to-server integrations). Scope names
# deliberately mirror dashboard.models.account.ApiKeyScope values so
# external_api.permissions.HasApiKeyScope can enforce either credential kind
# with the same required_scopes declarations.
OAUTH2_PROVIDER = {
    "PKCE_REQUIRED": True,
    # The native app's redirect targets: its custom scheme on Android/iOS, and
    # RFC 8252 loopback (any port - django-oauth-toolkit matches loopback IPs
    # port-insensitively) on desktop. "https" stays for any future web client.
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http", "urbanlens"],
    # Mirrors dashboard.models.account.model.ApiKeyScope verbatim (value ->
    # label). The duplication is unavoidable - settings load before the app
    # registry, so this module cannot import a model - and
    # test_external_api_scopes asserts the two stay identical.
    "SCOPES": {
        "profile:read": "Read your profile UUID",
        "settings:read": "Read your account preferences",
        "settings:write": "Change your account preferences",
        "pins:read": "Read your pins (including deletions, for sync)",
        "pins:write": "Create, edit, and delete your pins",
        "lists:read": "Read your pin lists and saved filters",
        "lists:write": "Create and modify your pin lists and saved filters",
        "labels:read": "Read your labels",
        "labels:write": "Create, modify, and merge your labels",
        "visits:read": "Read your visit history",
        "visits:write": "Log visits on your behalf",
        "photos:read": "Read your photos, memories journal, and photo suggestions",
        "photos:write": "Upload, label, file, vote on, and delete your photos, and act on photo suggestions",
        "media:read": "Fetch the actual image/video/document files you may see",
        "wiki:read": "Read community wikis you can see",
        "wiki:write": "Edit community wikis on your behalf",
        "trips:read": "Read your trips",
        "trips:write": "Create and edit your trips",
        "social:read": "Read your friends list and friend requests",
        "social:write": "Send, accept, and manage friend relationships on your behalf",
        "safety:read": "Read your safety check-ins and contacts",
        "safety:write": "Start, update, and clear safety check-ins",
        "messages:read": "Read your encrypted messages and conversation list",
        "messages:write": "Send messages and manage your encryption keys",
        "notifications:read": "Read your notifications and delivery preferences",
        "notifications:write": "Mark notifications read and change delivery preferences",
        "search:read": "Search your pins, wikis, and photos",
        "games:read": "Read your game history, scores, and leaderboard standing",
        "games:write": "Start games and submit guesses and answers on your behalf",
        "push:manage": "Register and remove this device's push notifications",
        "custom_fields:read": "Read your custom field definitions and their values",
        "custom_fields:write": "Create, edit, and delete your custom fields and their values",
        "undo:read": "Read your recent delete history available to undo",
        "undo:write": "Restore a previously deleted item",
        "panels:read": "Read pin-detail enrichment panels (boundaries and other plugin-contributed data)",
        "assistant:write": "Chat with your AI assistant, including creating trips and trip activities it suggests",
        "device_scans:read": "Read nearby expected devices and their signal info",
        "device_scans:write": "Upload wireless device scan data",
    },
    # Deliberately NOT the full SCOPES list: a token that asked for nothing in
    # particular gets the same minimal grant a PAT does
    # (account.model._default_api_key_scopes). Everything else must be
    # explicitly requested so it appears on the consent screen.
    "DEFAULT_SCOPES": ["profile:read", "pins:read", "pins:write", "push:manage"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 60 * 60 * 24 * 90,
    "ROTATE_REFRESH_TOKEN": True,
}

LOG_DIR = os.getenv("UL_LOG_DIR", os.path.join(PROJECT_ROOT, "logs"))
_log_file_path = os.path.join(LOG_DIR, "django.log")
_log_handlers = ["console"]
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    # Actually probe that the log file can be opened - makedirs succeeding
    # doesn't guarantee it (e.g. a broken symlink or a read-only mount).
    with open(_log_file_path, "a"):
        pass
except OSError:
    pass
else:
    _log_handlers.append("file")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} [{module}:{lineno}] {message}",
            "style": "{",
        },
    },
    "filters": {
        "health_check_access": {
            "()": "urbanlens.UrbanLens.logging_filters.HealthCheckAccessLogFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": _log_file_path,
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": _log_handlers,
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": _log_handlers,
            "level": "INFO",
            "propagate": False,
        },
        # Full tracebacks for unhandled view exceptions (5xx responses).
        "django.request": {
            "handlers": _log_handlers,
            "level": "ERROR",
            "propagate": False,
        },
        # ASGI/WSGI dev-server access loggers - silence the health check
        # probe's request line specifically, since it fires every ~30s.
        "django.channels.server": {
            "handlers": _log_handlers,
            "filters": ["health_check_access"],
            "level": "INFO",
            "propagate": False,
        },
        "django.server": {
            "handlers": _log_handlers,
            "filters": ["health_check_access"],
            "level": "INFO",
            "propagate": False,
        },
        "urbanlens": {
            "handlers": _log_handlers,
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}
