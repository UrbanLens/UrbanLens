from __future__ import annotations

import logging
import logging.config
import os
from typing import TYPE_CHECKING, Any

import yaml
from yaml.loader import SafeLoader

from .exceptions import FileEmptyError

if TYPE_CHECKING:
    from .meta import SettingsFile, SettingsLog

SETTINGS_PATH: str = "../conf/settings.yaml"

# Add placeholders for new API keys and endpoints
INSTAGRAM_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_GRAPH_URL = "your-instagram-graph-url-placeholder"
GOOGLE_LENS_API_KEY = os.environ.get("GOOGLE_LENS_API_KEY", "")
GOOGLE_LENS_URL = "your-google-lens-url-placeholder"


class Settings:
    """Settings for /bin scripts, loaded from bin/conf/settings.yaml."""

    _settings: SettingsFile | None = None
    _logging_setup: bool = False

    @classmethod
    def settings(cls) -> SettingsFile:
        return cls._settings or cls.load_config()

    @classmethod
    def logging(cls) -> SettingsLog:
        return cls.settings().get("logging")

    @classmethod
    def get_logger(cls, namespace: str):
        """Return the module logger, setting up logging once."""
        if cls._logging_setup is not True:
            # dictConfig allows TypedDicts, but mypy doesn't know that.
            logging.config.dictConfig(Settings.logging())  # type: ignore[arg-type]
            cls._logging_setup = True

        return logging.getLogger(namespace)

    @classmethod
    def load_config(cls) -> SettingsFile:
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_PATH)

        if os.path.exists(filepath):
            with open(filepath, encoding="utf-8") as file:
                cls._settings = yaml.load(file, Loader=SafeLoader)
        else:
            raise FileNotFoundError(f"Could not load bin settings from {filepath}")

        # Validate contents of settings file.
        if not cls._settings:
            raise FileEmptyError(f"No data in settings file at f{filepath}")

        return cls._settings

    @classmethod
    def all(cls) -> SettingsFile:
        """Return the settings dict.

        Returns:
            SettingsFile: A dictionary of settings.
        """
        return cls.settings()

    @classmethod
    def get(cls, key: str) -> Any:
        """Return the value at key.

        Args:
            key: A key to retrieve.

        Returns:
            The value stored at the provided key.
        """
        return cls.settings().get(key)


if __name__ == "__main__":
    conf = Settings.settings()
    print(conf)
