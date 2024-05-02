"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    app.py                                                                                               *
*        Path:    /UrbanLens/settings/app.py                                                                           *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from pathlib import Path
import logging
from typing import Any, Optional, Self

from pydantic import Field
from pydantic_core import Url
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic._internal._model_construction import ModelMetaclass
from django import conf
from django.conf import LazySettings

from UrbanLens.environments.types import EnvironmentTypes
from UrbanLens.environments.factory import select_environment
from UrbanLens.environments.base import BaseEnvironment
from UrbanLens.settings.meta.app import DEFAULT_PATH_PARENTS, DEFAULT_ROOT

logger = logging.getLogger(__name__)

class AppSettingsMeta(ModelMetaclass):
    """
    Metaclass to ensure only one instance of the class is created
    """
    _instances = {}

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
    project_name: str = Field(default='URBANLENS', description="The name of the project")
    environment_name: str = Field(default=EnvironmentTypes.LOCAL, description="The name of the environment")
    debug_override: Optional[bool] = Field(description="Whether or not to enable debugging", alias='DEBUG', default=None)
    secret_key : str = Field(default = '1t5v24s98-fcbas23-vfsd238vc-asfdioj322', description = "The secret key")
    root_urlconf : str = Field(default = 'UrbanLens.urls', description = "The root urlconf")
    admin_username : str = Field(default = 'Admin', description = "The username to use for the admin user")
    admin_email : str = Field(default = 'jess@urbanlens.org', description = "The email to use for the admin user")
    allowed_hosts : list[str] = Field(default = ['urbanlens.org'], description = "The allowed hosts")
    language_code : str = Field(default = 'en-us', description = "The language code")
    time_zone : str = Field(default = 'EST', description = "The time zone")
    use_i18n : bool = Field(default = True, description = "Whether or not to use i18n")
    use_tz : bool = Field(default = True, description = "Whether or not to use tz")
    email_from : str = Field(default = 'jess@urbanlens.org', description = "The from email")
    email_host : str = Field(default = 'smtp.gmail.com', description = "The email host")
    email_port : int = Field(default = 587, description = "The email port")
    backup_retention : int = Field(default = 30, description = "The number of days to retain backups")

    # Classes
    default_auto_field : str = Field(default = 'django.db.models.BigAutoField', description = "The default auto field")
    wsgi_application : str = Field(default = 'UrbanLens.wsgi.application', description = "The wsgi application")
    asgi_application : str = Field(default = 'UrbanLens.asgi.application', description = "The asgi application")
    test_runner : str = Field(default = 'core.tests.runner.TestRunner', description = "The test runner")

    # Urls
    login_url : str = Field(default = 'login', description = "The login url")
    static_url : str = Field(default = 'static/', description = "The static url")

    # Directory settings
    base_dir: Path = Field(default='UrbanLens', description="The name of the base directory")
    media_root: Path = Field(default='downloads', description="The name of the media directory")
    downloads_dir : Path = Field(default = 'downloads', description = "The name of the downloads directory")
    backups_dir : Path = Field(default = 'backups', description = "The name of the backups directory")
    log_root: Path = Field(default='logs', description="The name of the log directory")
    exports_dir : Path = Field(default = 'exports', description = "The name of the exports directory")
    static_root : Path = Field(default = 'frontend/static', description = "The name of the static directory")

    # APIs
    cloudflare_ai_endpoint : Url = Field(default='', description = "The cloudflare ai endpoint")
    cloudflare_worker_ai_endpoint : Url = Field(default='', description = "The cloudflare worker ai endpoint")
    cloudflare_ai_api_key : str = Field(default='', description = "The cloudflare ai key")
    huggingface_ai_endpoint : Url = Field(default='', description = "The huggingface ai endpoint")
    huggingface_ai_api_key : str = Field(default='', description = "The huggingface ai key")
    openai_api_key : str = Field(default='', description = "The openai key")
    google_places_api_key : str = Field(default='', description = "The google places key")
    google_maps_api_key : str = Field(default='', description = "The google maps key")
    google_search_api_key : str = Field(default='', description = "The google search key")
    google_search_tenant : str = Field(default='', description = "The google search tenant")
    smithsonian_api_key : str = Field(default='', description = "The smithsonian key")
    google_client_id : str = Field(default='', description = "The google client id")
    google_client_secret : str = Field(default='', description = "The google client secret")
    openweathermap_api_key : str = Field(default='', description = "The openweathermap key")
    nps_api_key : str = Field(default='', description = "The national park service api key")

    # DB
    database_engine : str = Field(default = 'psqlextra.backend', description = "The database engine")
    database_host : str = Field(default = 'localhost', description = "The database host")
    database_port : str = Field(default = '5432', description = "The database port")
    database_name : str = Field(default = 'urbanlens', description = "The database name")
    database_user : str = Field(default = 'urbanlens', description = "The database user")
    database_pass : str = Field(default = 'urbanlens', description = "The database password")

    _secrets : Optional[dict] = None
    _environment : Optional[BaseEnvironment] = None

    model_config = SettingsConfigDict(
        env_file=Path(DEFAULT_ROOT, '.env'),
        env_prefix='UL_',
        str_strip_whitespace = True
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
    def debug(self, value : bool) -> None:
        """
        Set the debug value
        """
        self.debug_override = value

        if self._environment:
            self._environment.debug_override = value

    @property
    def environment(self) -> Optional[BaseEnvironment]:
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
            'project_root': self.project_root,
            'base_dir': self.base_dir,
            'media_root': self.media_root,
            'downloads_dir': self.downloads_dir,
            'backups_dir': self.backups_dir,
            'log_root': self.log_root,
            'exports_dir': self.exports_dir,
            'static_root': self.static_root,
        }

    @property
    def databases(self) -> dict[str, dict[str, str]]:
        return conf.settings.DATABASES

    @property
    def logging(self) -> dict[str, Any]:
        return conf.settings.LOGGING

    @property
    def django(self) -> LazySettings:
        return conf.settings

    @property
    def MEDIA_ROOT(self) -> Path:
        return self.media_root

    @property
    def BASE_DIR(self) -> Path:
        return self.base_dir

    @property
    def ENVIRONMENT(self) -> BaseEnvironment:
        return self.environment

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ensure_paths()

    def get_secret(self, key : str, default : Optional[Any] = None) -> Any:
        """
        Get the secret from the secrets dictionary

        Args:
            key (str): The key to retrieve
            default (Any, optional): The default value to return if the key is not found. Defaults to None.

        Returns:
            Any: The value of the secret
        """
        if not self.secrets:
            return default

        # Break key into parts based on '.' Each part is a key in the previous key's value
        parts = key.split('.')
        value = self._secrets
        for part in parts:
            if part in value:
                value = value[part]
            else:
                return default

        return value or default

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
                    parent = getattr(self, DEFAULT_PATH_PARENTS[key])
                    value = Path(parent, value)
                    setattr(self, key, value)

                if not value.exists():
                    # If the path contains a period, infer it is a file, and don't create it
                    if '.' not in value.name:
                        value.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        value.mkdir(parents=True, exist_ok=True)
            except FileNotFoundError:
                logger.error(f"Error ensuring path: {key} - {value}")

        # Ensure app.log, debugging.log, and test.log exist in log dir
        for filename in ['app.log', 'debugging.log', 'test.log']:
            if not self.log_root.exists():
                self.log_root.mkdir(parents=True, exist_ok=True)
            filepath = Path(self.log_root, filename)
            if not filepath.exists():
                with open(filepath, 'w') as file:
                    file.write('')

    def refresh_django(self):
        """
        Refresh django.conf.settings with the current settings
        """
        for key, value in self.__dict__.items():
            # Filter out settings we don't want to propogate back to django
            if key.startswith('_') or key in ['model_config', 'paths', 'secrets', 'databases', 'logging', 'django']:
                continue

            const_name = key.upper()
            setattr(conf.settings, const_name, value)

        # Refresh properties as well
        conf.settings.DEBUG = self.debug
        conf.settings.ENVIRONMENT = self.environment

        # Refresh django.conf
        '''
        conf.settings.configure(
            default_settings = self
        )
        '''

    def select_environment(self, new_environment_name : Optional[EnvironmentTypes] = None) -> BaseEnvironment:
        """
        Select the environment

        Args:
            new_environment_name (Optional[EnvironmentTypes], optional):
                The name of the environment to switch to. Defaults to None.

        Returns:
            BaseEnvironment: The environment to use
        """
        if self._environment is not None and self.environment_name == new_environment_name:
            return self._environment

        if new_environment_name:
            self.environment_name = new_environment_name
            logger.info(f"Switching to environment: {new_environment_name}")
        self._environment = select_environment(self.environment_name)

        # If we set debug_override to any bool, propogate it to the new env
        if self.debug_override is True or self.debug_override is False:
            self._environment.debug_override = self.debug_override

        self.refresh_django()
        return self._environment

    def __getattr__(self, name : str):
        """
        Get an attribute that is fully uppercase (from django settings) as a parameter here (as lowercase)
        """
        key = name.lower()
        if key in self.__dict__:
            return self.__dict__[key]

        return super().__getattr__(name)

settings = AppSettings()