"""Django's ``migrate``, after :data:`~urbanlens.dashboard.plugins.points.BEFORE_MIGRATE`."""

from __future__ import annotations

from typing import Any

from django.core.management.commands import migrate
from django.db import DEFAULT_DB_ALIAS


class Command(migrate.Command):
    def execute(self, *args: Any, **options: Any) -> str | None:
        """Let plugins prepare the database, then migrate it."""
        if not (options.get("plan") or options.get("check_unapplied")):
            from urbanlens.dashboard.plugins import points

            points.BEFORE_MIGRATE.fire(options.get("database") or DEFAULT_DB_ALIAS)
        return super().execute(*args, **options)
