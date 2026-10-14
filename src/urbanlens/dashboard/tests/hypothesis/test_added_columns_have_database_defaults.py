"""
A column a migration adds to an existing table has a database default, so the release before it can keep writing.

A deploy migrates while the previous release's processes still run, and their inserts name only the columns they know.
A NOT NULL column with only a Python default refuses each of them: on dev, an upgrade window logged
``null value in column "relevance_rule" of relation "dashboard_location_cache" violates not-null constraint``.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import connection, migrations, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AddField, CreateModel

from urbanlens.core.tests.testcase import TestCase

_PROJECT_APPS = ("dashboard", "core")


def _project_migrations() -> list[tuple[str, Migration]]:
    loaded = MigrationLoader(None, ignore_no_migrations=True).disk_migrations.items()
    return [(app_label, migration) for (app_label, _name), migration in loaded if app_label in _PROJECT_APPS]


def _added_not_null_columns(project_migrations: Iterable[tuple[str, Migration]]) -> set[tuple[str, str]]:
    """``(table, column)`` for every NOT NULL column a migration adds to a table it did not create, and the models still have."""
    added: set[tuple[str, str]] = set()
    for app_label, migration in project_migrations:
        if migration.initial:
            continue
        created = {operation.name_lower for operation in migration.operations if isinstance(operation, CreateModel)}
        for operation in migration.operations:
            if not isinstance(operation, AddField) or operation.field.null or operation.field.many_to_many:
                continue
            if operation.model_name_lower in created:
                continue
            try:
                model = apps.get_model(app_label, operation.model_name)
                field = model._meta.get_field(operation.name)
            except (LookupError, FieldDoesNotExist):
                continue
            if getattr(field, "column", None) and not field.null:
                added.add((model._meta.db_table, field.column))
    return added


class AddedColumnsHaveDatabaseDefaultsTests(TestCase):
    def test_every_added_not_null_column_has_a_database_default(self) -> None:
        added = _added_not_null_columns(_project_migrations())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_default IS NOT NULL"
            )
            defaulted = set(cursor.fetchall())

        self.assertEqual(sorted(added - defaulted), [])

    def test_the_scan_finds_a_column_added_to_an_existing_table(self) -> None:
        """Guards the guard: the initial migration creates every table, so until a later one adds a column the scan above finds none."""
        later = Migration("0002_probe", "dashboard")
        later.operations = [
            migrations.AddField("locationcache", "relevance_rule", models.PositiveSmallIntegerField(default=0)),
            migrations.AddField("locationcache", "query_key", models.CharField(max_length=255, null=True)),
        ]

        self.assertEqual(
            _added_not_null_columns([("dashboard", later)]), {("dashboard_location_cache", "relevance_rule")}
        )

    def test_the_previous_release_can_still_cache_a_panel_and_log_a_notification(self) -> None:
        """The inserts v0.8.0 makes, naming only the columns it knew."""
        from model_bakery import baker

        location = baker.make("dashboard.Location", latitude=41.7, longitude=-73.9, google_place=None)
        profile = baker.make("auth.User").profile
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO dashboard_location_cache (created, updated, source, data, query_key, location_id) "
                "VALUES (now(), now(), 'v080_source', '{}', '', %s)",
                [location.pk],
            )
            cursor.execute("SELECT relevance_rule, audience FROM dashboard_location_cache WHERE source = 'v080_source'")
            self.assertEqual(cursor.fetchone(), (0, ""))
            cursor.execute(
                "INSERT INTO dashboard_notifications (created, updated, status, importance, notification_type, title, "
                "message, url, profile_id, uuid) VALUES (now(), now(), 'unread', 'lowest', 'info', '', 'v080', '', %s, "
                "gen_random_uuid())",
                [profile.pk],
            )
            cursor.execute("SELECT fold_key, fold_count FROM dashboard_notifications WHERE message = 'v080'")
            self.assertEqual(cursor.fetchone(), ("", 1))
