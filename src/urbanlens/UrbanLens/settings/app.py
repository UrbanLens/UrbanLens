from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Self

from django import conf
from django.conf import LazySettings
from django.core.management.utils import get_random_secret_key

from pydantic import Field, field_validator, model_validator
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import Url
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.factory import select_environment
from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes
from urbanlens.UrbanLens.settings.meta.app import DEFAULT_PATH_PARENTS, DEFAULT_ROOT

logger = logging.getLogger(__name__)

# DEFAULT_ROOT is src/, but .env lives one level up at the project root.
# List both so either location works; later entry wins if both exist.
_ENV_FILE_PATHS = [
    Path(DEFAULT_ROOT, ".env"),
    Path(DEFAULT_ROOT.parent, ".env"),
]


def _default_allowed_hosts() -> list[str]:
    """Local dev defaults to a wildcard-friendly host list; other environments lock down to the canonical domain."""
    if os.getenv("UL_ENVIRONMENT", EnvironmentTypes.LOCAL).lower() == EnvironmentTypes.LOCAL:
        return ["urbanlens.org", "localhost", "127.0.0.1"]
    return ["urbanlens.org"]


class AppSettingsMeta(ModelMetaclass):
    """
    Metaclass to ensure only one instance of the class is created
    """

    _instances: dict[type, Any] = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class AppSettings(BaseSettings, metaclass=AppSettingsMeta):
    """
    Class to hold settings for the application.
    """

    project_root: Path = Field(default=DEFAULT_ROOT, description="The root directory of the project")
    project_name: str = Field(default="URBANLENS", description="The name of the project")
    app_version: str = Field(default="", description="Semantic application version from pyproject.toml")
    environment_name: str = Field(default=EnvironmentTypes.LOCAL, description="The name of the environment")
    debug_override: bool | None = Field(description="Whether or not to enable debugging", alias="DEBUG", default=None)
    secret_key: str = Field(default=get_random_secret_key(), description="The secret key")
    root_urlconf: str = Field(default="urbanlens.UrbanLens.urls", description="The root urlconf")
    admin_username: str = Field(default="Admin", description="The username to use for the admin user")
    admin_email: str = Field(default="admin@yourdomain.com", description="The email to use for the admin user")
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=_default_allowed_hosts, description="The allowed hosts")
    plugin_modules: Annotated[list[str], NoDecode] = Field(default_factory=list, description="Dotted module paths of additional UrbanLens plugins to load (comma-separated)")
    disabled_plugins: Annotated[list[str], NoDecode] = Field(default_factory=list, description="Names of discovered UrbanLens plugins to disable for this install (comma-separated)")
    language_code: str = Field(default="en-us", description="The language code")
    time_zone: str = Field(default="EST", description="The time zone")
    use_i18n: bool = Field(default=True, description="Whether or not to use i18n")
    use_tz: bool = Field(default=True, description="Whether or not to use tz")
    email_backend: str = Field(default="django.core.mail.backends.console.EmailBackend", description="Django email backend class path")
    email_from: str = Field(default="noreply@yourdomain.com", description="The from email")
    email_host: str = Field(default="smtp.gmail.com", description="The email host")
    email_port: int = Field(default=587, description="The email port")
    email_user: str | None = Field(default=None, description="SMTP username / sending address")
    email_password: str | None = Field(default=None, description="SMTP password or app password")
    email_tls: bool = Field(default=True, description="Use STARTTLS (port 587)")
    email_use_ssl: bool = Field(default=False, description="Use SSL instead of STARTTLS (port 465)")
    backup_enabled: bool = Field(default=True, description="Whether scheduled database backups are enabled")
    backup_frequency_hours: int = Field(default=24, description="How often scheduled database backups should run, in hours")
    backup_retention: int = Field(default=30, description="The number of backup files to retain")
    allow_dev_toolbar_for_non_admins: bool = Field(
        default=False,
        description=(
            "Allow authenticated users without site-admin permission to see the developer toolbar. "
            "Only takes effect in development, local, or testing environments - ignored in staging/production."
        ),
    )

    # Classes
    default_auto_field: str = Field(default="django.db.models.BigAutoField", description="The default auto field")
    wsgi_application: str = Field(default="urbanlens.UrbanLens.wsgi.application", description="The wsgi application")
    asgi_application: str = Field(default="urbanlens.UrbanLens.asgi.application", description="The asgi application")
    test_runner: str = Field(default="urbanlens.core.tests.runner.TestRunner", description="The test runner")

    # Urls
    login_url: str = Field(default="login", description="The login url")
    static_url: str = Field(default="static/", description="The static url")

    # Directory settings
    base_dir: Path = Field(default=Path("urbanlens"), description="The name of the base directory")
    media_root: Path = Field(default=Path("downloads"), description="The name of the media directory")
    downloads_dir: Path = Field(default=Path("downloads"), description="The name of the downloads directory")
    backups_dir: Path = Field(default=Path("backups"), description="The name of the backups directory")
    log_root: Path = Field(default=Path("logs"), description="The name of the log directory")
    exports_dir: Path = Field(default=Path("exports"), description="The name of the exports directory")
    static_root: Path = Field(default=Path("frontend/static"), description="The name of the static directory")

    # APIs
    cloudflare_ai_endpoint: Url | None = Field(default=None, description="The cloudflare ai endpoint")
    cloudflare_worker_ai_endpoint: Url | None = Field(default=None, description="The cloudflare worker ai endpoint")
    cloudflare_ai_api_key: str | None = Field(default=None, description="The cloudflare ai key")
    huggingface_ai_endpoint: Url | None = Field(default=None, description="The huggingface ai endpoint")
    huggingface_ai_api_key: str | None = Field(default=None, description="The huggingface ai key")
    openai_api_key: str | None = Field(default=None, description="The openai key")
    google_unrestricted_api_key: str | None = Field(default=None, description="The google unrestricted api key")
    google_domain_restricted_api_key: str | None = Field(default=None, description="The google domain restricted api key")
    google_public_api_key: str | None = Field(default=None, description="The google public api key")
    google_search_tenant: str | None = Field(default=None, description="The google search tenant")
    google_client_id: str | None = Field(default=None, description="The google client id")
    google_client_secret: str | None = Field(default=None, description="The google client secret")
    apple_maps_api_key: str | None = Field(default=None, description="The apple maps JWT (pre-generated from Apple Developer private key)")
    usgs_api_key: str | None = Field(default=None, description="The USGS M2M application token (from EarthExplorer account settings)")
    usgs_username: str | None = Field(default=None, description="The USGS EarthExplorer username (required alongside usgs_api_key for M2M auth)")
    mapbox_api_key: str | None = Field(default=None, description="The Mapbox public access token (pk.* token)")
    bing_maps_api_key: str | None = Field(default=None, description="The Bing Maps API key (from Azure portal)")
    mapillary_access_token: str | None = Field(default=None, description="The Mapillary client access token")
    brave_search_api_key: str | None = Field(default=None, description="The Brave Search API key")
    smithsonian_api_key: str | None = Field(default=None, description="The smithsonian key")
    openweathermap_api_key: str | None = Field(default=None, description="The openweathermap key")
    nps_api_key: str | None = Field(default=None, description="The national park service api key")
    discord_client_secret: str | None = Field(default=None, description="The discord client secret")
    discord_client_id: str | None = Field(default=None, description="The discord client ID")

    # DB
    database_engine: str = Field(default="psqlextra.backend", description="The database engine")
    database_host: str = Field(default="localhost", description="The database host")
    database_port: str = Field(default="5432", description="The database port")
    database_name: str = Field(default="urbanlens", description="The database name")
    database_user: str = Field(default="urbanlens", description="The database user")
    database_pass: str = Field(default="urbanlens", description="The database password")

    _secrets: dict | None = None
    _environment: BaseEnvironment | None = None

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE_PATHS,
        env_prefix="UL_",
        str_strip_whitespace=True,
        extra="ignore",
    )

    @property
    def debug(self) -> bool:
        """
        Whether or not debugging is enabled
        """
        if self._environment:
            return self._environment.debug
        # This is only used prior to the environment being set.
        # -- after that, it is propogated to the environment
        return self.debug_override or False

    @debug.setter
    def debug(self, value: bool) -> None:
        """
        Set the debug value
        """
        self.debug_override = value

        if self._environment:
            self._environment.debug_override = DebugTypes.OVERRIDE_ON if value else DebugTypes.OVERRIDE_OFF

    @property
    def environment(self) -> BaseEnvironment | None:
        """
        The environment
        """
        return self._environment

    @property
    def secrets(self) -> dict:
        """
        The secrets dictionary
        """
        return self._secrets or {}

    @property
    def paths(self) -> dict[str, Path]:
        """
        Returns a dictionary of directories
        """
        return {
            "project_root": self.project_root,
            "base_dir": self.base_dir,
            "media_root": self.media_root,
            "downloads_dir": self.downloads_dir,
            "backups_dir": self.backups_dir,
            "log_root": self.log_root,
            "exports_dir": self.exports_dir,
            "static_root": self.static_root,
        }

    @property
    def databases(self) -> dict[str, dict[str, Any]]:
        """Django DATABASES configuration.

        Values are heterogeneous: most keys map to strings, but keys like
        ``TEST`` and ``OPTIONS`` hold nested dicts.
        """
        return conf.settings.DATABASES

    @property
    def logging(self) -> dict[str, Any]:
        return conf.settings.LOGGING

    @property
    def django(self) -> LazySettings:
        return conf.settings

    @field_validator("allowed_hosts", "plugin_modules", "disabled_plugins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: Any) -> Any:
        """Allow list-valued settings to be provided as comma-separated strings via env vars."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _resolve_version_metadata(self) -> Self:
        """Populate version metadata from pyproject.toml when not configured."""
        from urbanlens.core.version import get_app_version

        if not self.app_version:
            self.app_version = get_app_version()
        return self

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._check_env_file()
        self.ensure_paths()

    def _check_env_file(self) -> None:
        for env_path in _ENV_FILE_PATHS:
            if env_path.exists():
                if env_path.stat().st_size == 0:
                    logger.warning("Found .env file but it is empty: %s", env_path)
                return
        logger.warning(
            ".env file not found; API keys and secrets will be missing. Checked: %s",
            ", ".join(str(p) for p in _ENV_FILE_PATHS),
        )

    def ensure_paths(self) -> None:
        """
        Ensure the directories are absolute and exist.
        """
        for key, value in self.paths.items():
            try:
                if not isinstance(value, Path):
                    value = Path(value)
                    setattr(self, key, value)

                if not value.is_absolute():
                    parent = getattr(self, str(DEFAULT_PATH_PARENTS[key]))
                    value = Path(parent, value)
                    setattr(self, key, value)

                if not value.exists():
                    # If the path contains a period, infer it is a file, and don't create it
                    if "." not in value.name:
                        value.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        value.mkdir(parents=True, exist_ok=True)
            except FileNotFoundError:
                logger.error("Error ensuring path: %s - %s", key, value)

        # Ensure app.log, debugging.log, and test.log exist in log dir
        for filename in ["app.log", "debugging.log", "test.log"]:
            if not self.log_root.exists():
                self.log_root.mkdir(parents=True, exist_ok=True)
            filepath = Path(self.log_root, filename)
            if not filepath.exists():
                Path(filepath).write_text("")

    def refresh_django(self):
        """
        Refresh django.conf.settings with the current settings
        """
        for key, value in self.__dict__.items():
            # Filter out settings we don't want to propogate back to django
            if key.startswith("_") or key in ["model_config", "paths", "secrets", "databases", "logging", "django"]:
                continue

            const_name = key.upper()
            setattr(conf.settings, const_name, value)

        # Refresh properties as well
        conf.settings.DEBUG = self.debug
        conf.settings.ENVIRONMENT = self.environment

        # Refresh django.conf
        """
        conf.settings.configure(
            default_settings = self
        )
        """

    def select_environment(self, new_environment_name: EnvironmentTypes | None = None) -> BaseEnvironment:
        """
        Select the environment

        Args:
            new_environment_name (EnvironmentTypes | None, optional):
                The name of the environment to switch to. Defaults to None.

        Returns:
            BaseEnvironment: The environment to use
        """
        if self._environment is not None and self.environment_name == new_environment_name:
            return self._environment

        if new_environment_name:
            self.environment_name = new_environment_name
            logger.info("Switching to environment: %s", new_environment_name)
        self._environment = select_environment(self.environment_name)

        # If we set debug_override to any bool, propogate it to the new env
        if self.debug_override is True or self.debug_override is False:
            self._environment.debug_override = DebugTypes.OVERRIDE_ON if self.debug_override else DebugTypes.OVERRIDE_OFF

        self.refresh_django()
        return self._environment

    def __getattr__(self, name: str):
        """
        Get an attribute that is fully uppercase (from django settings) as a parameter here (as lowercase)
        """
        key = name.lower()
        if key in self.__dict__:
            return self.__dict__[key]

        return super().__getattr__(name)


settings = AppSettings()
