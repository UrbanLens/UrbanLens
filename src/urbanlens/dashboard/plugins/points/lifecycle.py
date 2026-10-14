"""Plugin lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ActionHook

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.registry import PluginRegistry


PLUGINS_LOADED: ActionHook[[PluginRegistry]] = ActionHook(
    "plugins.loaded",
    "Fired once every active plugin has been set up. Receives the plugin registry.",
)

__all__ = ["PLUGINS_LOADED"]
