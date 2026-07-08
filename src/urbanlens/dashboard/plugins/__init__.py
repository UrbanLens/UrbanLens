"""UrbanLens plugin framework.

External integrations (third-party APIs and the features built on them) are
packaged as plugins so individual installs can add, remove, and disable them
independently. See :mod:`urbanlens.dashboard.plugins.base` for how to write
one and :mod:`urbanlens.dashboard.plugins.registry` for how they are
discovered.
"""

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.plugins.hooks import HookRegistry, hooks
from urbanlens.dashboard.plugins.registry import PluginInfo, PluginRegistry, plugin_registry

__all__ = [
    "HookRegistry",
    "PluginInfo",
    "PluginRegistry",
    "UrbanLensPlugin",
    "hooks",
    "plugin_registry",
]
