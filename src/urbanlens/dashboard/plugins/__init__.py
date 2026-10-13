"""
UrbanLens plugin framework.
External integrations (third-party APIs and the features built on them) are packaged as plugins so individual installs can add, remove, and disable them independently."""

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.plugins.hooks import HookRegistry, hooks
from urbanlens.dashboard.plugins.registry import PluginInfo, PluginRegistry, plugin_registry

__all__ = [
    "HookRegistry",
    "PluginInfo",
    "PluginRegistry",
    "UrbanLensPlugin",
    "hooks",
    "plugin_available",
    "plugin_registry",
]


def plugin_available(name: str) -> bool:
    """
    Whether a plugin with slug `name` was discovered in this install.

    Discovery runs in the app's ``ready()``, so this is only meaningful after
    Django setup - which is always the case for requests, tasks, and collected
    tests. It says nothing about ``UL_DISABLED_PLUGINS``; templates deciding
    whether a route exists want the generic ``plugin_names`` context variable
    instead.

    Args:
        name: The plugin slug.

    Returns:
        True when a plugin with that slug was discovered.
    """
    return plugin_registry.get(name) is not None
