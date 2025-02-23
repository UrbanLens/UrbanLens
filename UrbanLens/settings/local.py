"""*********************************************************************************************************************
*                                                                                                                      *
*        Django settings for urbanlens project.

*        Generated by 'django-admin startproject' using Django 4.2.2.

*        For more information on this file, see
*        https://docs.djangoproject.com/en/4.2/topics/settings/

*        For the full list of settings and their values, see
*        https://docs.djangoproject.com/en/4.2/ref/settings/
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    local.py                                                                                             *
*        Path:    /UrbanLens/settings/local.py                                                                         *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-r-8lxm+kdnd+j)-lxp7bdr8w260+7#d$j%&6l6g^3)3ly*()wb"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['urbanlens.org', 'localhost']

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
    'dashboard.apps.DashboardConfig',
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

ROOT_URLCONF = "UrbanLens.urls"

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
                'dashboard.context_processors.add_page_name',
            ],
        },
    },
]

WSGI_APPLICATION = "UrbanLens.wsgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    "default": {
		"ENGINE": os.getenv("UL_DATABASE_ENGINE", "django.contrib.gis.db.backends.postgis"),
		'NAME': os.getenv("UL_DATABASE_NAME", 'urbanlens'),
		'USER': os.getenv("UL_DATABASE_USER", 'urbanlens'),
		'PASSWORD': os.getenv("UL_DATABASE_PASS"),
		'HOST': os.getenv("UL_DATABASE_HOST", 'localhost'),
		'PORT': os.getenv("UL_DATABASE_PORT", '5432'),
    },
}
DATABASE_ROUTERS = ['dashboard.dbrouters.DBRouter']


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

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# http://urbanlens.org, http://urbanlens.com, https://urbanlens.org, https://urbanlens.com, etc
protocols = ['http://', 'https://']
domains = ['urbanlens.org', 'urbanlens.com', 'localhost:6464']
subdomains = ['www.', '']
CORS_ALLOWED_ORIGINS = [
    f'{protocol}{subdomain}{domain}'
    for protocol in protocols 
    for subdomain in subdomains 
    for domain in domains
]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS.copy()

SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.getenv("UL_GOOGLE_CLIENT_ID", "")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.getenv("UL_GOOGLE_CLIENT_SECRET", "")
SOCIAL_AUTH_DISCORD_KEY = os.getenv("UL_DISCORD_KEY", "")
SOCIAL_AUTH_DISCORD_SECRET = os.getenv("UL_DISCORD_SECRET", "")
SMITHSONIAN_API_KEY = os.getenv("UL_SMITHSONIAN_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("UL_GOOGLE_PLACES_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("UL_GOOGLE_PLACES_API_KEY", "")
GOOGLE_SEARCH_API_KEY = os.getenv("UL_GOOGLE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_TENANT = os.getenv("UL_GOOGLE_SEARCH_CX", "")
OPEN_WEATHER_API_KEY = os.getenv("UL_OPENWEATHERMAP_API_KEY", "")
NPS_API_KEY = os.getenv("UL_NPS_API_KEY", "")