"""Environment-driven settings a plugin declares for itself."""

from __future__ import annotations

from typing import Self

from pydantic_settings import BaseSettings, SettingsConfigDict

from urbanlens.UrbanLens.settings.app import AppSettings


class PluginSettings(BaseSettings):
    """
    A plugin's own settings, read from ``UL_``-prefixed environment variables and ``.env`` like core's.

    Subclass it in the plugin with typed fields and read it through :meth:`get`, which caches one instance per class::

        class StorefrontSettings(PluginSettings):
            api_key: str | None = None  # UL_STOREFRONT_API_KEY

            model_config = PluginSettings.config(prefix="UL_STOREFRONT_")

    Core settings never name a plugin's variables, so an install without the plugin has none of them.
    """

    model_config = SettingsConfigDict(
        env_file=AppSettings.model_config.get("env_file"),
        env_prefix="UL_",
        str_strip_whitespace=True,
        env_ignore_empty=True,
        extra="ignore",
    )

    @classmethod
    def config(cls, *, prefix: str) -> SettingsConfigDict:
        """The base configuration with another environment-variable prefix."""
        return SettingsConfigDict(**{**PluginSettings.model_config, "env_prefix": prefix})

    @classmethod
    def get(cls) -> Self:
        """The cached instance, read from the environment on first use."""
        # Read from the class's own namespace, so a subclass never receives its parent's instance.
        instance = cls.__dict__.get("_instance")
        if not isinstance(instance, cls):
            instance = cls()
            setattr(cls, "_instance", instance)  # noqa: B010 - pydantic models reject undeclared class attributes
        return instance

    @classmethod
    def reload(cls) -> Self:
        """Read the environment again, for tests that change it."""
        if "_instance" in cls.__dict__:
            delattr(cls, "_instance")
        return cls.get()
