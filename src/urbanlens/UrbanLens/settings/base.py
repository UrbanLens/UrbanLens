from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from celery.schedules import crontab
from django.core.management.utils import get_random_secret_key
from dotenv import find_dotenv, load_dotenv

from urbanlens.UrbanLens.settings._env import is_production_environment

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent

# Find the repo-root .env regardless of working directory.
load_dotenv(find_dotenv())

ENVIRONMENT_NAME = os.getenv("UL_ENVIRONMENT", "local").lower()
_is_local = ENVIRONMENT_NAME == "local"
_is_dev = ENVIRONMENT_NAME in {"local", "development"}

# Positive fail-closed production check; `not _is_dev` also matches typos/staging.
IS_PRODUCTION = is_production_environment(ENVIRONMENT_NAME)

# SECURITY WARNING: keep the secret key used in production secret!
#
# The random fallback would orphan encrypted-field data across processes, so fail loudly where a real DB is in play.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ""
if not SECRET_KEY:
    # Ephemeral keys are only safe with no durable encrypted data (dev/test).
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


# pytest-django skips DiscoverRunner's HTTPS-redirect disable, so detect tests here too.
TESTING = _env_bool("DJANGO_TESTING", False) or any(
    arg.endswith("pytest") or "pytest" in arg for arg in sys.argv
)

DEBUG = _env_bool("DJANGO_DEBUG", _is_dev)

# AppSettings owns ALLOWED_HOSTS (UL_ALLOWED_HOSTS); local defaults allow immediate access.
from urbanlens.UrbanLens.settings import _metrics  # noqa: E402
from urbanlens.UrbanLens.settings._env import env_bool  # noqa: E402
from urbanlens.UrbanLens.settings.app import settings as _app_settings  # noqa: E402

ALLOWED_HOSTS = _app_settings.allowed_hosts

# Application definition
INSTALLED_APPS = [
    # Before staticfiles so runserver serves WebSockets via Channels in dev.
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
    # Registers django-csp checks (e.g. csp.E001 on legacy setting format).
    "csp",
    "urbanlens.dashboard.apps.DashboardConfig",
    "social_django",
    # OAuth2 provider for native clients; browser sessions/ApiKeys unaffected.
    "oauth2_provider",
    # OpenAPI schema for the external API surface only.
    "drf_spectacular",
]

# Prometheus counters, only on processes that serve /metrics.
UL_METRICS_ENABLED = _app_settings.metrics_enabled
UL_METRICS_TOKEN = _app_settings.metrics_token
UL_METRICS_ALLOWED_CIDRS = _app_settings.metrics_allowed_cidrs

# Only scraped roles pay for instrumentation; see settings/_metrics.py.
UL_METRICS_INSTRUMENTED = _metrics.instrumentation_wanted(metrics_enabled=UL_METRICS_ENABLED, process_role=_app_settings.process_role)

if UL_METRICS_INSTRUMENTED:
    _metrics.require_django_prometheus()
    INSTALLED_APPS.append("django_prometheus")

# ASGI for websockets; production HTTP stays on WSGI/gunicorn.
ASGI_APPLICATION = "urbanlens.UrbanLens.asgi.application"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "urbanlens.dashboard.middleware.SecurityHeadersMiddleware",
    # Attaches the CSP header to every response, including short-circuited ones.
    "csp.middleware.CSPMiddleware",
    # Above CommonMiddleware so redirects/preflights carry CORS headers.
    "corsheaders.middleware.CorsMiddleware",
    # Serves STATIC_ROOT where no nginx fronts the app; no-op behind compose nginx.
    #
    # Short-circuits in the request phase: layers above still touch the response,
    # layers below (sessions, auth, cookies) are skipped, keeping it cacheable.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Reports user id; logs wall/CPU/SQL for requests over UL_SLOW_REQUEST_MS.
    "urbanlens.dashboard.middleware.RequestTelemetryMiddleware",
    # Mints the media-origin cookie; no-op unless UL_MEDIA_BASE_URL is set.
    "urbanlens.dashboard.middleware.MediaOriginCookieMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Innermost: swaps in the ghost viewer for profile previews.
    "urbanlens.dashboard.middleware.ProfilePreviewMiddleware",
    # Records the viewer the request actually acts as.
    "urbanlens.dashboard.middleware.WriteSourceMiddleware",
]

if UL_METRICS_INSTRUMENTED:
    # Outermost/innermost pair per django-prometheus docs; the delta is stack cost.
    MIDDLEWARE.insert(0, "django_prometheus.middleware.PrometheusBeforeMiddleware")
    MIDDLEWARE.append("django_prometheus.middleware.PrometheusAfterMiddleware")

# Narrower than django-prometheus defaults, straddling actual serve times.
# 120 matches nginx proxy_read_timeout; keep in step.
PROMETHEUS_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float("inf"))

# Off: /health/ready already reports migration state to the prober.
PROMETHEUS_EXPORT_MIGRATIONS = False

AUTHENTICATION_BACKENDS = [
    "social_core.backends.google.GoogleOAuth2",
    "social_core.backends.discord.DiscordOAuth2",
    "urbanlens.dashboard.services.auth.auth_backend.EmailOrUsernameModelBackend",
]

ROOT_URLCONF = "urbanlens.UrbanLens.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Before APP_DIRS so our registration templates win over admin/auth.
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
                "urbanlens.dashboard.context_processors.add_keyboard_shortcuts",
                "urbanlens.dashboard.context_processors.add_direct_messages",
                "urbanlens.dashboard.context_processors.add_demo_context",
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
        # Persistent connections for deployments reaching the DB over high-latency links.
        "CONN_MAX_AGE": int(os.getenv("UL_DB_CONN_MAX_AGE", "0")),
        "CONN_HEALTH_CHECKS": os.getenv("UL_DB_CONN_HEALTH_CHECKS", "").lower() in {"1", "true", "yes"},
        # Fail fast on unreachable DB so a request errors instead of holding a worker.
        "OPTIONS": {
            "connect_timeout": int(os.getenv("UL_DB_CONNECT_TIMEOUT", "10")),
            # Labels this tier in pg_stat_activity for pool attribution.
            "application_name": f"urbanlens-{os.getenv('UL_PROCESS_ROLE', 'unknown')}",
        },
        # UL_TEST_DB_NAME isolates concurrent test runs to separate databases.
        "TEST": {"NAME": os.getenv("UL_TEST_DB_NAME") or None},
    },
}
# Valkey/Redis for pin payloads and Django cache when configured.
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
    # Write-through sessions survive a cache flush.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"

    # Cross-process group messaging. socket_timeout stays above BRPOP's 5s timeout.
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
                # Per-run prefix isolates concurrent test runs sharing group names.
                **({"prefix": f"asgi-test-{os.getenv('UL_TEST_DB_NAME', 'default')}"} if TESTING else {}),
            },
        },
    }

DATABASE_ROUTERS = ["urbanlens.dashboard.dbrouters.DBRouter"]

# Celery defaults to Valkey/Redis, else local Redis for dev.
CELERY_BROKER_URL = os.getenv("UL_CELERY_BROKER_URL") or VALKEY_URL or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = os.getenv("UL_CELERY_RESULT_BACKEND") or CELERY_BROKER_URL
# Bound result-backend recovery retries to fail fast when the broker is down.
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {"retry_policy": {"timeout": 5.0}}
# Keep above max(time_limit, longest countdown) to avoid duplicate delivery.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 2 * 60 * 60}
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = os.getenv("UL_CELERY_TIMEZONE", "UTC")
CELERY_TASK_ALWAYS_EAGER = os.getenv("UL_CELERY_TASK_ALWAYS_EAGER", "False").lower() in {"true", "1", "yes"}
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SEND_SENT_EVENT = True
# Events feed the celery-metrics exporter; off when metrics are off.
CELERY_WORKER_SEND_TASK_EVENTS = UL_METRICS_ENABLED
CELERY_TASK_ACKS_LATE = True
# Off: a child killed by OOM/segfault would otherwise requeue unboundedly with no failure event.
CELERY_TASK_REJECT_ON_WORKER_LOST = False
# Pinned default; False with ACKS_LATE requeues every over-limit task. Guarded by E007/E008.
CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("UL_CELERY_TASK_SOFT_TIME_LIMIT", "2700"))
CELERY_TASK_TIME_LIMIT = int(os.getenv("UL_CELERY_TASK_TIME_LIMIT", "3600"))
# Recycle workers to bound RSS growth from long-lived C-extension imports.
CELERY_WORKER_MAX_TASKS_PER_CHILD = int(os.getenv("UL_CELERY_WORKER_MAX_TASKS_PER_CHILD", "200"))
CELERY_WORKER_MAX_MEMORY_PER_CHILD = int(os.getenv("UL_CELERY_WORKER_MAX_MEMORY_PER_CHILD", str(512 * 1024)))  # KiB

# Sandbox tier; see services/sandbox/guard.py and docs/MEDIA_PIPELINE.md.
UL_PROCESS_ROLE = _app_settings.process_role
# Threshold for RequestTelemetryMiddleware's slow-request log. See settings/app.py.
UL_SLOW_REQUEST_MS = _app_settings.slow_request_ms
UL_SANDBOX_ENABLED = _app_settings.sandbox_enabled
UL_UNTRUSTED_PARSE_POLICY = _app_settings.untrusted_parse_policy
# AI inference sandbox tier - see services/sandbox/guard.py's
# DirectInferencePolicy and docs/AI_PIPELINE.md for the deployment topology.
UL_AI_INFERENCE_URL = _app_settings.ai_inference_url
UL_AI_INFERENCE_TOKEN = _app_settings.ai_inference_token
UL_AI_INFERENCE_TIMEOUT_SECONDS = _app_settings.ai_inference_timeout_seconds
UL_DIRECT_INFERENCE_POLICY = _app_settings.direct_inference_policy
UL_AI_WORKER_ENABLED = _app_settings.ai_worker_enabled
# Backup defaults, overridable in the database-backed settings UI.
UL_BACKUP_ENABLED = os.getenv("UL_BACKUP_ENABLED", "True").lower() in {"true", "1", "yes"}
UL_BACKUP_FREQUENCY_HOURS = int(os.getenv("UL_BACKUP_FREQUENCY_HOURS", "24"))
UL_BACKUP_RETENTION = int(os.getenv("UL_BACKUP_RETENTION", "30"))

# Leaflet zoom at/above which a MarkupMap viewport counts every visible pin as shared.
UL_MAP_SHARE_ZOOM_THRESHOLD = float(os.getenv("UL_MAP_SHARE_ZOOM_THRESHOLD", "14"))

# Hourly work is staggered so same-interval entries don't stampede one queue.
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
    # Unconditional, like every entry above: the task itself checks UL_DEMO_MODE
    # and returns immediately everywhere else. See docs/DEMO.md.
    "scheduled-demo-account-purge": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_demo_account_purge",
        "schedule": crontab(minute=27),
    },
    # Unconditional too - see run_scheduled_redata_public_locations_sync.
    "scheduled-redata-public-locations-sync": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_redata_public_locations_sync",
        "schedule": crontab(minute=32, hour="*/6"),
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
    # Catches date-passed thresholds and lost signal enqueues.
    "achievements-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_achievements",
        "schedule": crontab(hour=3, minute=10),
    },
    # Recovers scoring rows lost to broker blips.
    "reputation-sweep": {
        "task": "urbanlens.dashboard.tasks.sweep_reputation",
        "schedule": crontab(hour=6, minute=10),
    },
    # Safety net for missed Stripe webhooks.
    "stripe-subscriptions-sync": {
        "task": "urbanlens.dashboard.tasks.sync_stripe_subscriptions",
        "schedule": crontab(hour=4, minute=10),
    },
    # Counts down banked access after cancel; webhooks stop firing then.
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
    # Backfills photos predating grid thumbnails.
    "image-thumbnail-backfill": {
        "task": "urbanlens.dashboard.tasks.backfill_image_thumbnails",
        "schedule": crontab(minute=4),
    },
    # Recovers uploads stuck pending_scan from a lost enqueue.
    "requeue-stalled-pending-uploads": {
        "task": "urbanlens.dashboard.tasks.requeue_stalled_pending_uploads",
        "schedule": crontab(minute=19),
    },
    # Daily: enforces a week-long retry window for abandoned failed uploads.
    "discard-unretried-failed-uploads": {
        "task": "urbanlens.dashboard.tasks.discard_unretried_failed_uploads",
        "schedule": crontab(minute=9, hour=4),
    },
    # Leftover preview sources from failed enqueues.
    "sweep-stale-preview-sources": {
        "task": "urbanlens.dashboard.tasks.sweep_stale_preview_sources",
        "schedule": crontab(minute=34),
    },
    # Map-marker thumbnail backfill; offset to avoid tick contention.
    "image-marker-thumbnail-backfill": {
        "task": "urbanlens.dashboard.tasks.backfill_image_marker_thumbnails",
        "schedule": crontab(minute=34),
    },
    # Analysis-copy backfill; gates AI keywording, so not merely cosmetic.
    "image-analysis-thumbnail-backfill": {
        "task": "urbanlens.dashboard.tasks.backfill_image_analysis_thumbnails",
        "schedule": crontab(minute=19),
    },
    # Daily; clients resync past any pruning gap via the 410 signal.
    "pin-tombstone-pruning": {
        "task": "urbanlens.dashboard.tasks.prune_pin_tombstones",
        "schedule": crontab(hour=5, minute=10),
    },
    # Daily; retention follows the costs page's 12-month chart.
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
# Filesystem default; 's3' points the same FileField API at object storage.
# GatedS3Storage keeps FileField.url on /media/ behind the gate.
UL_MEDIA_STORAGE_BACKEND = _app_settings.media_storage_backend.strip().lower()

_S3_STORAGE_OPTIONS = {
    "bucket_name": _app_settings.s3_bucket_name,
    "endpoint_url": _app_settings.s3_endpoint_url or None,
    "access_key": _app_settings.s3_access_key_id,
    "secret_key": _app_settings.s3_secret_access_key,
    "region_name": _app_settings.s3_region_name,
    "addressing_style": _app_settings.s3_addressing_style,
    # Private bucket: no per-object ACL, no direct serving.
    "default_acl": None,
    "querystring_auth": True,
    # Never overwrite; differs from S3Storage's default.
    "file_overwrite": False,
    "signature_version": "s3v4",
}

# Manifest storage needs collectstatic; tests use plain storage.
STORAGES = {
    "default": (
        {"BACKEND": "urbanlens.dashboard.services.media.object_storage.GatedS3Storage", "OPTIONS": _S3_STORAGE_OPTIONS}
        if UL_MEDIA_STORAGE_BACKEND == "s3"
        else {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    ),
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if TESTING else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Own origin isolates uploads from session cookies; empty keeps same-origin /media/.
UL_MEDIA_BASE_URL = _app_settings.media_base_url.rstrip("/")
UL_MEDIA_COOKIE_DOMAIN = _app_settings.media_cookie_domain
# Media-origin CSP minus frame-ancestors; empty uses the service default.
UL_MEDIA_CSP = os.getenv("UL_MEDIA_CSP", "")

MEDIA_URL = f"{UL_MEDIA_BASE_URL}/media/" if UL_MEDIA_BASE_URL else "/media/"
MEDIA_ROOT = os.path.join(PROJECT_ROOT, "media")

# Gate answers with X-Accel-Redirect behind nginx, streams directly in dev.
MEDIA_X_ACCEL = _env_bool("UL_MEDIA_X_ACCEL", not _is_dev)
# Must match the nginx `location /_protected_media/` block.
MEDIA_X_ACCEL_PREFIX = "/_protected_media/"

# Object-store X-Accel prefix; empty streams through Django. Needs its own nginx location.
MEDIA_X_ACCEL_OBJECT_PREFIX = _app_settings.media_x_accel_object_prefix

# Short-lived: consumed by nginx within the minting request, plus clock skew.
MEDIA_X_ACCEL_OBJECT_URL_TTL_SECONDS = 60

# Ingress body cap, enforced early so users get an app error, not a proxy page.
MAX_REQUEST_BODY_BYTES = max(0, _app_settings.max_request_body_mb) * 1_000_000
MAP_DOCUMENT_MAX_PINS = _app_settings.map_document_max_pins
MAP_DOCUMENT_CACHE_SECONDS = _app_settings.map_document_cache_seconds

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Plain HTTP only for local/dev by default; override via UL_UNSAFE_ALLOW_HTTP.
_http_default = "True" if _is_dev else "False"
UNSAFE_ALLOW_HTTP = _env_bool("UL_UNSAFE_ALLOW_HTTP", _http_default == "True")
SECURE_SSL_REDIRECT = not UNSAFE_ALLOW_HTTP and not TESTING
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", SECURE_SSL_REDIRECT)
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", SECURE_SSL_REDIRECT)
# Container health checks hit /health over HTTP.
SECURE_REDIRECT_EXEMPT = [r"^health"]

# HSTS mirrors SECURE_SSL_REDIRECT; preload left off for self-hosted domains.
SECURE_HSTS_SECONDS = int(os.getenv("UL_HSTS_SECONDS", "31536000")) if SECURE_SSL_REDIRECT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("UL_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_SECONDS > 0)

# No Django setting exists for these; consumed by SecurityHeadersMiddleware.
PERMISSIONS_POLICY = "geolocation=(self), clipboard-write=(self), camera=(), microphone=(), payment=(), usb=(), interest-cohort=(), browsing-topics=()"
# same-site covers sibling origins under the parent domain.
CROSS_ORIGIN_RESOURCE_POLICY = "same-site"
X_PERMITTED_CROSS_DOMAIN_POLICIES = "none"

# Report-only: `require-corp` would break paste-any-URL overlays; `credentialless` fails open where unsupported.
CROSS_ORIGIN_EMBEDDER_POLICY_REPORT_ONLY = "credentialless"

# Content-Security-Policy (django-csp >= 4).
#
# Tile hosts need wildcard and bare forms for Leaflet's {s} expansion.
#
# script-src 'unsafe-inline' is load-bearing (inline scripts, hx-on:, json_script); migration needs a nonce everywhere at once.
_CSP_DIRECTIVES: dict[str, object] = {
    "default-src": ["'self'"],
    # CDN scripts plus runtime-injected Maps API.
    "script-src": [
        "'self'",
        "'unsafe-inline'",
        "https://code.jquery.com",
        "https://cdnjs.cloudflare.com",
        "https://unpkg.com",
        "https://cdn.jsdelivr.net",
        "https://maps.googleapis.com",
    ],
    # Inline styles and Leaflet runtime positioning.
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
        # Paste-any-URL overlays need any HTTPS host; images don't execute.
        "https:",
        # Base map tiles and overlays.
        "https://*.tile.openstreetmap.org",
        "https://tile.openstreetmap.org",
        "https://*.basemaps.cartocdn.com",
        "https://basemaps.cartocdn.com",
        "https://*.tile.opentopomap.org",
        "https://tile.opentopomap.org",
        "https://server.arcgisonline.com",
        "https://services.arcgisonline.com",
        "https://tile.openweathermap.org",
        # Favicons and avatar preview.
        "https://www.google.com",
        "https://www.gravatar.com",
        # Maps imagery hosts (runtime-chosen; report-only reveals gaps).
        "https://maps.googleapis.com",
        "https://maps.gstatic.com",
    ],
    # ws: for plain-HTTP local/dev game sockets.
    "connect-src": [
        "'self'",
        "ws:",
        "wss:",
        # Unproxied browser geocoding and inline place summaries.
        "https://nominatim.openstreetmap.org",
        "https://en.wikipedia.org",
        "https://maps.googleapis.com",
    ],
    # Street View embed.
    "frame-src": ["'self'", "https://www.google.com"],
    "media-src": ["'self'", "data:", "blob:"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    # DENY-equivalent; use 'none' to keep the stricter X-Frame-Options posture.
    "frame-ancestors": ["'self'"],
    "form-action": ["'self'"],
}

# A vendor mirror must be admitted or UL_CSP_ENFORCE drops those assets.
def allow_vendor_mirror(directives: dict[str, object], base_url: object) -> str | None:
    """Admit a vendor-asset mirror origin.

    Args:
        directives: The CSP directive lists, modified in place.
        base_url: The configured mirror root, or a falsy value for none.

    Returns:
        The origin admitted, or None when unconfigured.
    """
    if not base_url:
        return None
    parsed = urlparse(str(base_url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for name in ("script-src", "style-src", "font-src"):
        hosts = directives.get(name)
        if isinstance(hosts, list) and origin not in hosts:
            hosts.append(origin)
    return origin


def allow_media_origin(directives: dict[str, object], base_url: str) -> str | None:
    """Admit the media origin where uploads are fetched (video, iframe, JS bytes).

    Args:
        directives: The CSP directive lists, modified in place.
        base_url: The configured media origin, or empty for same-origin.

    Returns:
        The origin admitted, or None when same-origin.
    """
    if not base_url:
        return None
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for name in ("img-src", "media-src", "frame-src", "connect-src"):
        hosts = directives.get(name)
        if isinstance(hosts, list) and origin not in hosts:
            hosts.append(origin)
    return origin


allow_vendor_mirror(_CSP_DIRECTIVES, _app_settings.vendor_asset_base_url)
allow_media_origin(_CSP_DIRECTIVES, UL_MEDIA_BASE_URL)

# Report-only default; UL_CSP_ENFORCE flips to blocking once reports are clean.
CSP_ENFORCE = _app_settings.csp_enforce
if CSP_ENFORCE:
    CONTENT_SECURITY_POLICY = {"DIRECTIVES": _CSP_DIRECTIVES}
else:
    CONTENT_SECURITY_POLICY_REPORT_ONLY = {"DIRECTIVES": _CSP_DIRECTIVES}

# Behind a TLS-terminating proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Proxy hops; read by per-IP rate limiters. See app.py field.
TRUSTED_PROXY_COUNT = _app_settings.trusted_proxy_count

# Per-connection WebSocket bounds; see app.py and frame_limits.py.
UL_WEBSOCKET_MAX_FRAME_CHARS = _app_settings.websocket_max_frame_chars
UL_WEBSOCKET_FRAMES_PER_MINUTE = _app_settings.websocket_frames_per_minute
UL_WEBSOCKET_FANOUT_FRAMES_PER_MINUTE = _app_settings.websocket_fanout_frames_per_minute
UL_MESSAGES_PER_MINUTE = _app_settings.messages_per_minute

# Transport bound derived at 4 bytes/char so it stays above the app bound.
UL_WEBSOCKET_MAX_MESSAGE_BYTES = UL_WEBSOCKET_MAX_FRAME_CHARS * 4

# Published app port; read from env so origins can't drift from compose.
_APP_PORT = os.getenv("UL_APP_PORT", "21800")

protocols = ["https://"]
if _is_local:
    domains = [
        "urbanlens.org",
        "localhost",
        "localhost:8000",
        f"localhost:{_APP_PORT}",
        "127.0.0.1",
        "127.0.0.1:8000",
        f"127.0.0.1:{_APP_PORT}",
        "[::1]",
        "[::1]:8000",
    ]
elif _is_dev:
    domains = ["urbanlens.org", "localhost", f"localhost:{_APP_PORT}", "127.0.0.1"]
else:
    domains = ["urbanlens.org", "localhost", f"localhost:{_APP_PORT}"]

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


def _origin_from_url(url: str) -> str | None:
    """Reduce a URL to its bare origin, or None when not absolute http(s).

    Args:
        url: Candidate URL.

    Returns:
        ``scheme://host[:port]`` (IPv6 re-bracketed), or None.
    """
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    # Skip malformed ALLOWED_HOSTS entries rather than minting bad origins.
    if not all(char.isascii() and (char.isalnum() or char in "-._:") for char in parsed.hostname):
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


def _derive_trusted_origins(allowed_hosts: list[str], site_url: str, *, allow_http: bool) -> tuple[list[str], list[str]]:
    """Derive trusted origins from hosts the deployment already serves.

    Args:
        allowed_hosts: ``ALLOWED_HOSTS`` for this deployment.
        site_url: ``UL_SITE_URL`` origin.
        allow_http: Whether plain-HTTP origins may be minted.

    Returns:
        ``(exact, wildcard)``; wildcards are CSRF-only.
    """
    schemes = ["https", "http"] if allow_http else ["https"]
    exact: list[str] = []
    wildcard: list[str] = []

    site_origin = _origin_from_url(site_url)
    if site_origin:
        exact.append(site_origin)

    for raw in allowed_hosts:
        entry = raw.strip().lower()
        if not entry or entry == "*":
            continue
        # Skip non-host entries that Django would never match.
        if any(char in entry for char in "/@?#"):
            continue
        # Treat `*.example.com` like Django's `.example.com`.
        covers_subdomains = entry.startswith((".", "*."))
        entry = entry.removeprefix("*").lstrip(".")
        if not entry or "*" in entry:
            continue
        # Bracket bare IPv6 literals.
        if entry.count(":") > 1 and not entry.startswith("["):
            entry = f"[{entry}]"
        for scheme in schemes:
            origin = _origin_from_url(f"{scheme}://{entry}")
            if origin is None:
                continue
            exact.append(origin)
            if covers_subdomains:
                wildcard.append(origin.replace("://", "://*.", 1))

    return list(dict.fromkeys(exact)), list(dict.fromkeys(wildcard))


# Read the env var directly: the SITE_URL fallback must not become trusted.
_derived_origins, _derived_wildcard_origins = _derive_trusted_origins(
    ALLOWED_HOSTS,
    os.getenv("UL_SITE_URL", ""),
    allow_http=UNSAFE_ALLOW_HTTP,
)
CORS_ALLOWED_ORIGINS = list(dict.fromkeys([*CORS_ALLOWED_ORIGINS, *_derived_origins]))
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([*CSRF_TRUSTED_ORIGINS, *_derived_origins, *_derived_wildcard_origins]))

SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.getenv("UL_GOOGLE_CLIENT_ID", "")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.getenv("UL_GOOGLE_CLIENT_SECRET", "")
SOCIAL_AUTH_DISCORD_KEY = os.getenv("UL_DISCORD_CLIENT_ID", "")
SOCIAL_AUTH_DISCORD_SECRET = os.getenv("UL_DISCORD_CLIENT_SECRET", "")
SOCIAL_AUTH_DISCORD_SCOPE = ["identify", "email"]

# Custom social-auth pipeline: provider handle, avatar, 2FA last.
SOCIAL_AUTH_PIPELINE = (
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "social_core.pipeline.social_auth.social_user",
    "urbanlens.dashboard.services.social_auth.pipeline.generate_sso_username",
    "social_core.pipeline.user.create_user",
    "social_core.pipeline.social_auth.associate_user",
    "social_core.pipeline.social_auth.load_extra_data",
    "social_core.pipeline.user.user_details",
    "urbanlens.dashboard.services.social_auth.pipeline.suppress_last_name_for_new_users",
    "urbanlens.dashboard.services.social_auth.pipeline.fetch_and_save_avatar",
    "urbanlens.dashboard.services.social_auth.pipeline.mark_new_user_onboarding",
    "urbanlens.dashboard.services.social_auth.pipeline.save_discord_social_link",
    "urbanlens.dashboard.services.social_auth.pipeline.enforce_two_factor_for_sso",
)

LOGIN_REDIRECT_URL = "/accounts/post-login/"
LOGIN_URL = "/accounts/login/"
LOGOUT_REDIRECT_URL = "/"

SOCIAL_AUTH_LOGIN_REDIRECT_URL = "/accounts/post-login/"
SOCIAL_AUTH_NEW_USER_REDIRECT_URL = "/accounts/post-login/"

EMAIL_BACKEND = os.getenv("UL_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("UL_EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("UL_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("UL_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("UL_EMAIL_PASSWORD", "")
# Lenient parse to match app.py's pydantic bools.
EMAIL_USE_TLS = env_bool("UL_EMAIL_TLS", default=True)
EMAIL_USE_SSL = env_bool("UL_EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = os.getenv("UL_EMAIL_FROM", "noreply@yourdomain.org")
# Base URL for absolute links from request-less contexts (e.g. Celery).
_site_url_env = os.getenv("UL_SITE_URL")
SITE_URL = _site_url_env or f"http://localhost:{_APP_PORT}"
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

# Authenticated users get burst/day limits; anonymous is tightly constrained.
REST_FRAMEWORK = {
    # All endpoints user-scoped; opt out per-view for public ones.
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
        # Per-credential tiers; reads/writes split so resync reads don't fund write loops.
        "external_api_read": "1000/hour",
        "external_api_write": "300/hour",
        "external_api_burst": "60/minute",
        # Gallery fetches dozens of files per screen; still capped against key leaks.
        "external_api_media": "2000/hour",
        # Endpoints whose cost scales with caller data (smart-list resync).
        "external_api_resync": "12/hour",
        # Per-keystroke autocomplete; separate so typing doesn't starve sync.
        "external_api_location_search": "1200/hour",
        # Billed game-start cost; resync-shaped cap.
        "external_api_game_start": "40/hour",
        # Multi-provider fan-out per call.
        "external_api_global_search": "300/hour",
        # Per-activity upstream calls on the request path.
        "external_api_calendar": "30/hour",
        # Billed multi-round-trip chat turns.
        "external_api_assistant_message": "60/hour",
        # Foreground tracking cadence, not ordinary writes.
        "external_api_safety_location": "360/hour",
    },
    # Only for views in the generated external-API schema.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # JSON only; avoids BrowsableAPIRenderer 500s and debug-page leaks.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# External API only; internal HTMX/REST surface excluded.
SPECTACULAR_SETTINGS = {
    "TITLE": "UrbanLens External API",
    "DESCRIPTION": "Versioned API for external applications and native clients holding a user's API key or OAuth2 token.",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "PREPROCESSING_HOOKS": ["urbanlens.dashboard.external_api.schema.preprocess_external_api_only"],
    # Replaces (not extends) defaults; keep the enum postprocessor.
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "urbanlens.dashboard.external_api.schema.document_error_responses",
    ],
    # Stable names so new choice fields don't renumber generated clients.
    "ENUM_NAME_OVERRIDES": {
        "SafetyCheckinStatusEnum": "urbanlens.dashboard.models.safety.model.SafetyCheckinStatus.choices",
        "SafetyCheckinPartnerStatusEnum": "urbanlens.dashboard.models.safety.model.SafetyCheckinPartnerStatus.choices",
        "FriendshipStatusEnum": "urbanlens.dashboard.models.friendship.meta.FriendshipStatus.choices",
        "TripActivityStatusEnum": "urbanlens.dashboard.models.trips.model.TripActivity.STATUS_CHOICES",
        "TripActivitySettableStatusEnum": ["proposed", "confirmed"],
        "LabelKindEnum": "urbanlens.dashboard.models.labels.meta.KIND_CHOICES",
    },
}

# Native-client auth; scopes mirror ApiKeyScope so both credentials share checks.
OAUTH2_PROVIDER = {
    "PKCE_REQUIRED": True,
    # Custom scheme + loopback for native apps; https for future web clients.
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http", "urbanlens"],
    # Duplicates ApiKeyScope (settings load before models; tests assert parity).
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
    # Minimal default grant matching PATs; rest need explicit consent.
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
    # Probe writability; makedirs alone doesn't prove it.
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
        # Full tracebacks for 5xx.
        "django.request": {
            "handlers": _log_handlers,
            "level": "ERROR",
            "propagate": False,
        },
        # Silence health-check probes in dev-server access logs.
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
