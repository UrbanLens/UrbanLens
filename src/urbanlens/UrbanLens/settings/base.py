from __future__ import annotations

import os
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

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()

# Detect the current environment early - other settings branch on it.
ENVIRONMENT_NAME = os.getenv("UL_ENVIRONMENT", "local").lower()
_is_local = ENVIRONMENT_NAME == "local"
_is_dev = ENVIRONMENT_NAME in {"local", "development"}


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
    "urbanlens.dashboard.apps.DashboardConfig",
    "social_django",
]

# Routes the websocket protocol (see UrbanLens/asgi.py); HTTP keeps using
# WSGI_APPLICATION in production (gunicorn) - only the dedicated `app-ws`
# daphne container and local `runserver` actually serve ASGI traffic.
ASGI_APPLICATION = "urbanlens.UrbanLens.asgi.application"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    # Innermost: swaps in the simulated viewer for "view profile as" previews.
    "urbanlens.dashboard.middleware.ProfilePreviewMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "social_core.backends.google.GoogleOAuth2",
    "social_core.backends.discord.DiscordOAuth2",
    "urbanlens.dashboard.services.auth_backend.EmailOrUsernameModelBackend",
]

ROOT_URLCONF = "urbanlens.UrbanLens.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
            },
        },
    }

DATABASE_ROUTERS = ["urbanlens.dashboard.dbrouters.DBRouter"]

# Celery - background job processing. Defaults to the configured Valkey/Redis
# endpoint when available, otherwise local Redis for development.
CELERY_BROKER_URL = os.getenv("UL_CELERY_BROKER_URL") or VALKEY_URL or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = os.getenv("UL_CELERY_RESULT_BACKEND") or CELERY_BROKER_URL
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

CELERY_BEAT_SCHEDULE = {
    "scheduled-database-backup-check": {
        "task": "urbanlens.dashboard.tasks.run_scheduled_database_backup",
        "schedule": 60 * 60,
    },
    "scheduled-vestigial-asset-cleanup": {
        "task": "urbanlens.dashboard.tasks.cleanup_vestigial_assets_task",
        "schedule": 60 * 60,
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
    "account-deletion-reminders": {
        "task": "urbanlens.dashboard.tasks.send_account_deletion_reminders",
        "schedule": 60 * 60,
    },
    "account-deletion-hard-delete": {
        "task": "urbanlens.dashboard.tasks.hard_delete_expired_accounts",
        "schedule": 60 * 60,
    },
    "safety-checkin-auto-delete": {
        "task": "urbanlens.dashboard.tasks.delete_expired_safety_checkins",
        "schedule": 60 * 60,
    },
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
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

# Trust the X-Forwarded-Proto header set by Nginx so Django builds https:// URLs
# when sitting behind a reverse proxy that terminates SSL.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

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
EMAIL_USE_TLS = os.getenv("UL_EMAIL_TLS", "True") == "True"
EMAIL_USE_SSL = os.getenv("UL_EMAIL_USE_SSL", "False") == "True"
DEFAULT_FROM_EMAIL = os.getenv("UL_EMAIL_FROM", "noreply@yourdomain.org")
# Canonical base URL used to build absolute links in emails/notifications sent
# from contexts with no HttpRequest to build them from (e.g. Celery tasks).
SITE_URL = os.getenv("UL_SITE_URL", "http://localhost:21080")
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
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "600/minute",
    },
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
        "urbanlens": {
            "handlers": _log_handlers,
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}
