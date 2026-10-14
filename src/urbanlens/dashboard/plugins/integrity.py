"""
Foreign keys from plugin tables into core tables cascade in the database, not only in Django.

Django runs ``on_delete`` in Python, and only for models it has loaded. A disabled plugin's tables stay in the
database, and so do tables an earlier release created for a feature this install does not have, so their rows would
make deleting the core row they point at fail. After every ``migrate`` this rewrites each such foreign key to act in
the database as the model declares it (``CASCADE`` or ``SET_NULL``), and makes a table no installed app owns
cascade. A key a plugin declares ``PROTECT``, ``RESTRICT`` or ``DO_NOTHING`` is left as Django made it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, connections, models

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger(__name__)

#: Postgres ``pg_constraint.confdeltype`` codes, and the clause each stands for.
ON_DELETE_SQL = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}

_FOREIGN_KEYS_INTO = """
    SELECT con.conname, src.relname, dst.relname, con.confdeltype, con.condeferrable, con.condeferred,
           ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(num, ord)
                 JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.num ORDER BY k.ord),
           ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(num, ord)
                 JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = k.num ORDER BY k.ord)
    FROM pg_constraint con
    JOIN pg_class src ON src.oid = con.conrelid
    JOIN pg_class dst ON dst.oid = con.confrelid
    JOIN pg_namespace ns ON ns.oid = src.relnamespace
    WHERE con.contype = 'f' AND ns.nspname = current_schema() AND dst.relname = ANY(%s) AND NOT (src.relname = ANY(%s))
"""


@dataclass(frozen=True, slots=True)
class ForeignKeyChange:
    """One foreign key rewritten to act in the database."""

    table: str
    constraint: str
    on_delete: str


def _plugin_app_labels() -> set[str]:
    from urbanlens.dashboard.plugins.registry import plugin_registry

    return {label for info in plugin_registry.plugins() if (label := info.app_label)}


def _wanted_action(model: type[models.Model] | None, columns: list[str]) -> str | None:
    """The ``confdeltype`` a key should have: the model field's ``on_delete``, or cascade for a table nothing owns."""
    if model is None:
        return "c"
    for field in model._meta.concrete_fields:  # noqa: SLF001 - Django's model metadata API
        if isinstance(field, models.ForeignKey) and [field.column] == columns:
            if field.remote_field.on_delete is models.CASCADE:
                return "c"
            if field.remote_field.on_delete is models.SET_NULL:
                return "n"
            return None
    return None


def ensure_foreign_keys_act_in_database(*, using: str = DEFAULT_DB_ALIAS) -> list[ForeignKeyChange]:
    """
    Rewrite every foreign key from a plugin's or an unowned table into a core table to act in the database.

    Idempotent; a key already acting as wanted is left alone.

    Returns:
        The keys rewritten.
    """
    connection: BaseDatabaseWrapper = connections[using]
    if connection.vendor != "postgresql":
        return []
    plugin_labels = _plugin_app_labels()
    owners: dict[str, type[models.Model]] = {model._meta.db_table: model for model in apps.get_models(include_auto_created=True)}  # noqa: SLF001
    core_tables = [table for table, model in owners.items() if model._meta.app_label not in plugin_labels]  # noqa: SLF001
    if not core_tables:
        return []

    changes: list[ForeignKeyChange] = []
    with connection.cursor() as cursor:
        cursor.execute(_FOREIGN_KEYS_INTO, [core_tables, core_tables])
        rows: list[tuple[Any, ...]] = cursor.fetchall()
        quote = connection.ops.quote_name
        for name, table, referenced, current, deferrable, deferred, columns, referenced_columns in rows:
            wanted = _wanted_action(owners.get(table), list(columns))
            if wanted is None or wanted == current:
                continue
            timing = (" DEFERRABLE INITIALLY DEFERRED" if deferred else " DEFERRABLE") if deferrable else ""
            cursor.execute(
                f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(name)}, "
                f"ADD CONSTRAINT {quote(name)} FOREIGN KEY ({', '.join(map(quote, columns))}) "
                f"REFERENCES {quote(referenced)} ({', '.join(map(quote, referenced_columns))}) ON DELETE {ON_DELETE_SQL[wanted]}{timing}",
            )
            changes.append(ForeignKeyChange(table=table, constraint=name, on_delete=ON_DELETE_SQL[wanted]))
    for change in changes:
        logger.info("Foreign key %s on %s now acts ON DELETE %s in the database", change.constraint, change.table, change.on_delete)
    return changes


def after_migrate(*, using: str = DEFAULT_DB_ALIAS, **_kwargs: Any) -> None:
    """``post_migrate`` receiver: keep plugin foreign keys acting in the database after every migration run."""
    ensure_foreign_keys_act_in_database(using=using)
