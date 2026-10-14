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

BEFORE_MIGRATE: ActionHook[[str]] = ActionHook(
    "migrations.before",
    "Fired by ``manage.py migrate`` with the database alias, before it reads the database's migration history or runs a system check, so a plugin can bring a database migrated under an earlier history in line with the current one. Not fired for ``--plan`` or ``--check``. A raising callback propagates and stops the run.",
    on_error="raise",
)

__all__ = ["BEFORE_MIGRATE", "PLUGINS_LOADED"]
