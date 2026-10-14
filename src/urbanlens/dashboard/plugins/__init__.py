"""
UrbanLens plugin framework.

Features and integrations an install may not want - third-party data sources, paid services, hosted infrastructure -
are plugins: core declares seams in :mod:`~urbanlens.dashboard.plugins.points` and reads whatever is contributed,
never naming a plugin. See ``docs/plugins/README.md``.
"""

from urbanlens.dashboard.plugins.base import PluginContext, UrbanLensPlugin
from urbanlens.dashboard.plugins.extensions import ActionHook, ExtensionPoint, FilterHook, Registry, TemplateSlot
from urbanlens.dashboard.plugins.registry import PluginInfo, PluginRegistry, PluginState, plugin_registry

__all__ = [
    "ActionHook",
    "ExtensionPoint",
    "FilterHook",
    "PluginContext",
    "PluginInfo",
    "PluginRegistry",
    "PluginState",
    "Registry",
    "TemplateSlot",
    "UrbanLensPlugin",
    "plugin_registry",
]
