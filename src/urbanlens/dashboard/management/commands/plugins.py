"""
List the plugins this install found, or uninstall one's tables.

    manage.py plugins                  # every plugin, its state, and its migrations
    manage.py plugins seams            # every seam and who contributes to it
    manage.py plugins uninstall NAME   # reverse NAME's migrations, dropping its tables
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from urbanlens.dashboard.plugins import plugin_registry
from urbanlens.dashboard.plugins.extensions import catalog
from urbanlens.dashboard.plugins.lifecycle import PluginNotInstalledError, migration_status, uninstall


class Command(BaseCommand):
    help = "List plugins and the seams they contribute to, or uninstall a plugin's tables."

    def add_arguments(self, parser: CommandParser) -> None:
        sub = parser.add_subparsers(dest="action")
        sub.add_parser("list", help="Every plugin, its state and its migrations (the default).")
        sub.add_parser("seams", help="Every seam core declares and what is contributed to it.")
        remove = sub.add_parser("uninstall", help="Reverse a package plugin's migrations, dropping its tables and rows.")
        remove.add_argument("name")
        remove.add_argument("--noinput", "--no-input", action="store_false", dest="interactive", help="Do not ask for confirmation.")

    def handle(self, *args: Any, **options: Any) -> None:
        action = options.get("action") or "list"
        if action == "seams":
            self._seams()
        elif action == "uninstall":
            self._uninstall(options["name"], interactive=options["interactive"], verbosity=options["verbosity"])
        else:
            self._list()

    def _list(self) -> None:
        for info in plugin_registry.plugins():
            line = f"{info.name:<32} {info.state:<20} {info.source.origin:<12} {info.source.module}"
            self.stdout.write(line)
            if info.detail:
                self.stdout.write(f"    {info.detail}")
            status = migration_status(info) if info.app_label else None
            if status and status.has_migrations:
                state = "current" if status.is_current else f"{len(status.pending)} pending: {', '.join(status.pending)}"
                self.stdout.write(f"    migrations: {len(status.applied)} applied, {state}")

    def _seams(self) -> None:
        for name, declared in sorted(catalog().items()):
            owners = ", ".join(owner or "core" for owner in declared.owners()) or "-"
            self.stdout.write(f"{name:<40} {declared.kind:<16} {owners}")

    def _uninstall(self, name: str, *, interactive: bool, verbosity: int) -> None:
        if interactive:
            answer = input(f"Drop every table of plugin {name!r} and all their rows? This cannot be undone. Type the plugin name to confirm: ")
            if answer.strip() != name:
                raise CommandError("Not confirmed; nothing was changed.")
        try:
            uninstall(name, verbosity=verbosity)
        except PluginNotInstalledError as exc:
            raise CommandError(f"{name!r} is not an installed package plugin, so it has no tables to drop.") from exc
        self.stdout.write(self.style.SUCCESS(f"Dropped {name}'s tables. Add it to UL_DISABLED_PLUGINS or remove its code, or the next migrate creates them again."))
