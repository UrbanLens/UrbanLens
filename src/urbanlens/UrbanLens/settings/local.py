from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
from django.core.management.utils import get_random_secret_key

# Build paths inside the project like this: BASE_DIR / 'subdir'.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env - search upward from this file so the
# repo-root .env is found regardless of working directory.
load_dotenv(find_dotenv())

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# this default is a dev-only settings module, never used in production
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['urbanlens.org', 'localhost', 'localhost:21080']

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "corsheaders",
    'urbanlens.dashboard.apps.DashboardConfig',
    "social_django",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
]

AUTHENTICATION_BACKENDS = [
    'social_core.backends.google.GoogleOAuth2',
    'social_core.backends.discord.DiscordOAuth2',
    'django.contrib.auth.backends.ModelBackend',
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
                'urbanlens.dashboard.context_processors.add_page_name',
                'urbanlens.dashboard.context_processors.add_site_settings',
                'urbanlens.dashboard.context_processors.add_dev_toolbar',
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
        'NAME': os.getenv("UL_DB_NAME", 'urbanlens'),
        'USER': os.getenv("UL_DB_USER", 'urbanlens'),
        'PASSWORD': os.getenv("UL_DB_PASS"),
        'HOST': os.getenv("UL_DB_HOST", 'localhost'),
        'PORT': os.getenv("UL_DB_PORT", '5432'),
    },
}
DATABASE_ROUTERS = ['urbanlens.dashboard.dbrouters.DBRouter']


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
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(PROJECT_ROOT, "media")

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# TODO: Potentially remove 'http', and only allow 'localhost' in dev.
# http://urbanlens.org, http://urbanlens.com, https://urbanlens.org, https://urbanlens.com, etc
protocols = ['http://', 'https://']
domains = ['urbanlens.org', 'localhost', 'localhost:21080']
subdomains = ['www.', '']
# Trust the X-Forwarded-Proto header set by Nginx so Django builds https:// URLs
# when sitting behind a reverse proxy that terminates SSL.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

CORS_ALLOWED_ORIGINS = [
    f'{protocol}{subdomain}{domain}'
    for protocol in protocols
    for subdomain in subdomains
    for domain in domains
]
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
SMITHSONIAN_API_KEY = os.getenv("UL_SMITHSONIAN_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("UL_GOOGLE_PLACES_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("UL_GOOGLE_MAPS_API_KEY", "")
GOOGLE_SEARCH_API_KEY = os.getenv("UL_GOOGLE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_TENANT = os.getenv("UL_GOOGLE_SEARCH_CX") or os.getenv("UL_GOOGLE_SEARCH_TENANT", "")
OPEN_WEATHER_API_KEY = os.getenv("UL_OPENWEATHERMAP_API_KEY", "")
NPS_API_KEY = os.getenv("UL_NPS_API_KEY", "")

TEST_RUNNER = 'urbanlens.core.tests.runner.TestRunner'
