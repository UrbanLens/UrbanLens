"""
Migration operations for plugins.

A plugin's models are its own app's, so its migrations are ordinary Django migrations: ``manage.py migrate`` creates
its tables where it is installed, and ``manage.py plugins uninstall <name>`` reverses them. These operations cover what
ordinary migrations cannot: taking over a table that an earlier release created under another app.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from django.db import migrations

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import ProjectState


class AdoptModel(migrations.CreateModel):
    """
    Create a model's table, or take over the table an earlier release created for it under another app.

    Where ``legacy_table`` exists it is renamed to the model's table and its content type relabelled, so its rows,
    permissions and generic relations carry over; elsewhere the table is created as :class:`CreateModel` would. The
    legacy table must already match the model's state at this migration - follow with ordinary migrations for any
    later change.

    Reversing renames the table back to ``legacy_table`` (and relabels the content type), whether or not it was
    adopted: a rollback that reverses this migration goes on to reverse the core migration that handed the model
    over, and that one expects the legacy table. Uninstalling the plugin (:func:`dropping_adopted_tables`) drops it.
    """

    def __init__(self, name: str, fields: list[tuple[str, Any]], *, legacy_table: str, legacy_app_label: str, **kwargs: Any) -> None:
        """
        Args:
            name: The model name.
            fields: As for :class:`CreateModel`.
            legacy_table: The table an earlier release created.
            legacy_app_label: The app that earlier release declared the model in.
            **kwargs: As for :class:`CreateModel`.
        """
        super().__init__(name, fields, **kwargs)
        self.legacy_table = legacy_table
        self.legacy_app_label = legacy_app_label

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        name, args, kwargs = super().deconstruct()
        kwargs["legacy_table"] = self.legacy_table
        kwargs["legacy_app_label"] = self.legacy_app_label
        return name, args, kwargs

    def reduce(self, operation: Any, app_label: str) -> bool:
        # Folding later operations into this one would hand them to a table that may not be the one they expect.
        return False

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        if not _table_exists(schema_editor, self.legacy_table):
            super().database_forwards(app_label, schema_editor, from_state, to_state)
            return
        schema_editor.alter_db_table(model, self.legacy_table, model._meta.db_table)  # noqa: SLF001 - Django's model metadata API
        _relabel_content_type(schema_editor, self.name_lower, from_label=self.legacy_app_label, to_label=app_label)

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        if _dropping.get() or self.legacy_app_label == app_label:
            super().database_backwards(app_label, schema_editor, from_state, to_state)
            return
        schema_editor.alter_db_table(model, model._meta.db_table, self.legacy_table)  # noqa: SLF001 - Django's model metadata API
        _relabel_content_type(schema_editor, self.name_lower, from_label=app_label, to_label=self.legacy_app_label)

    def describe(self) -> str:
        return f"Create model {self.name}, or adopt {self.legacy_table}"

    @property
    def migration_name_fragment(self) -> str:
        return f"adopt_{self.name_lower}"


_dropping: ContextVar[bool] = ContextVar("dropping_adopted_tables", default=False)


@contextmanager
def dropping_adopted_tables() -> Iterator[None]:
    """Within the block, reversing :class:`AdoptModel` drops its table instead of handing it back to the legacy app."""
    token = _dropping.set(True)
    try:
        yield
    finally:
        _dropping.reset(token)


def _table_exists(schema_editor: BaseDatabaseSchemaEditor, table: str) -> bool:
    with schema_editor.connection.cursor() as cursor:
        return table in schema_editor.connection.introspection.table_names(cursor)


def _relabel_content_type(schema_editor: BaseDatabaseSchemaEditor, model: str, *, from_label: str, to_label: str) -> None:
    """Move a model's content type between apps, unless the target label already has one."""
    if not _table_exists(schema_editor, "django_content_type"):
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE django_content_type SET app_label = %s WHERE app_label = %s AND model = %s AND NOT EXISTS (SELECT 1 FROM django_content_type WHERE app_label = %s AND model = %s)",
            [to_label, from_label, model, to_label, model],
        )
