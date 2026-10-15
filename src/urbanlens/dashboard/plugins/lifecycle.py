"""
Installing and uninstalling package plugins' database tables.

A package plugin's migrations run with ``manage.py migrate`` like any app's. Disabling a plugin or removing its code
leaves its tables and their rows untouched, so enabling it again carries on where it left off; dropping them is a
deliberate :func:`uninstall` while the plugin is still installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.loader import MigrationLoader

from urbanlens.dashboard.plugins.migrations import dropping_adopted_tables

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.registry import PluginInfo


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """
    How far a package plugin's migrations have run.

    Attributes:
        applied: Names of applied migrations.
        pending: Names of migrations not yet applied, in plan order.
    """

    applied: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def has_migrations(self) -> bool:
        """Whether the plugin has any migrations at all."""
        return bool(self.applied or self.pending)

    @property
    def is_current(self) -> bool:
        """Whether every migration has run."""
        return not self.pending


class PluginNotInstalledError(LookupError):
    """The named plugin is not an installed package plugin, so it has no tables to manage."""


def migration_status(info: PluginInfo, *, database: str = DEFAULT_DB_ALIAS) -> MigrationStatus | None:
    """
    The migration state of one package plugin's app.

    Returns:
        Its status, or None for a module plugin or one whose app is not installed (it is disabled).
    """
    from django.apps import apps

    label = info.app_label
    if label is None or not apps.is_installed(info.source.module):
        return None
    loader = MigrationLoader(connections[database], ignore_no_migrations=True)
    nodes = [key for key in loader.graph.leaf_nodes() if key[0] == label]
    plan = [key for leaf in nodes for key in loader.graph.forwards_plan(leaf) if key[0] == label]
    ordered = list(dict.fromkeys(plan))
    applied = tuple(name for app, name in ordered if (app, name) in loader.applied_migrations)
    pending = tuple(name for app, name in ordered if (app, name) not in loader.applied_migrations)
    return MigrationStatus(applied=applied, pending=pending)


def uninstall(name: str, *, database: str = DEFAULT_DB_ALIAS, verbosity: int = 1) -> None:
    """
    Reverse every migration of one package plugin, dropping its tables and their rows.

    The plugin must still be installed (its code present and not disabled), since reversing needs its migrations.
    Afterwards, disable it or remove its code, or the next ``migrate`` creates its tables again.

    Raises:
        PluginNotInstalledError: When ``name`` is not an installed package plugin.
    """
    from urbanlens.dashboard.plugins.registry import plugin_registry

    info = plugin_registry.info(name)
    status = migration_status(info, database=database) if info else None
    if info is None or status is None:
        raise PluginNotInstalledError(name)
    if status.applied:
        with dropping_adopted_tables():
            call_command("migrate", info.app_label, "zero", database=database, verbosity=verbosity, interactive=False)
