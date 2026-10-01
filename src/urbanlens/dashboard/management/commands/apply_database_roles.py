"""Converge the per-tier Postgres login roles. Run as the owner; `docker compose` runs it in `db-setup`."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from urbanlens.dashboard.services.core.database_roles import DatabaseRoleError, app_role_password, apply_database_roles, declared_roles


class Command(BaseCommand):
    """Create or update every tier's login role, connection limit, and deadlines."""

    help = "Create or update the per-tier Postgres login roles and their connection budgets."

    def handle(self, *args: Any, **options: Any) -> None:
        """Apply the declared roles and report each one.

        Raises:
            CommandError: The roles could not be applied as declared.
        """
        try:
            applied = apply_database_roles(declared_roles(), app_role_password(str(connection.settings_dict.get("PASSWORD") or "")))
        except DatabaseRoleError as exc:
            raise CommandError(str(exc)) from exc
        for entry in applied:
            role = entry.role
            self.stdout.write(
                f"{role.name:14} {'created' if entry.created else 'updated':8} connection limit {role.connection_limit:>3}, deadline {role.deadline_seconds}s",
            )
