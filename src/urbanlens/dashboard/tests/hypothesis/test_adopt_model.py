"""AdoptModel takes over a legacy table, hands it back on a rollback, and drops it on an uninstall."""

from __future__ import annotations

from django.db import connection, models
from django.db.migrations.state import ProjectState

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.migrations import AdoptModel, dropping_adopted_tables

_APP = "adopttest"
_LEGACY_APP = "adoptlegacy"


def _operation() -> AdoptModel:
    return AdoptModel(
        "Widget",
        [("id", models.AutoField(primary_key=True)), ("label", models.CharField(max_length=20))],
        legacy_table="adoptlegacy_widgets",
        legacy_app_label=_LEGACY_APP,
        options={"db_table": "adopttest_widget"},
    )


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def _content_type_app() -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT app_label FROM django_content_type WHERE model = 'widget' AND app_label IN (%s, %s)",
            [_APP, _LEGACY_APP],
        )
        row = cursor.fetchone()
    return row[0] if row else None


class AdoptModelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.operation = _operation()
        self.before = ProjectState()
        self.after = self.before.clone()
        self.operation.state_forwards(_APP, self.after)

    def _forwards(self) -> None:
        with connection.schema_editor() as editor:
            self.operation.database_forwards(_APP, editor, self.before, self.after)

    def _backwards(self) -> None:
        with connection.schema_editor() as editor:
            self.operation.database_backwards(_APP, editor, self.after, self.before)

    def _make_legacy_table(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE adoptlegacy_widgets (id serial PRIMARY KEY, label varchar(20) NOT NULL)")
            cursor.execute("INSERT INTO adoptlegacy_widgets (label) VALUES ('kept')")
            cursor.execute("INSERT INTO django_content_type (app_label, model) VALUES (%s, 'widget')", [_LEGACY_APP])

    def test_without_a_legacy_table_it_creates_the_model_table(self) -> None:
        self._forwards()
        self.assertIn("adopttest_widget", _tables())
        self.assertNotIn("adoptlegacy_widgets", _tables())

    def test_a_legacy_table_is_renamed_with_its_rows_and_content_type(self) -> None:
        self._make_legacy_table()
        self._forwards()

        self.assertNotIn("adoptlegacy_widgets", _tables())
        with connection.cursor() as cursor:
            cursor.execute("SELECT label FROM adopttest_widget")
            self.assertEqual(cursor.fetchall(), [("kept",)])
        self.assertEqual(_content_type_app(), _APP)

    def test_a_rollback_hands_the_table_back_to_the_legacy_app(self) -> None:
        """The core migration reversed next expects the legacy table, whether or not it was adopted."""
        for adopted in (True, False):
            with self.subTest(adopted=adopted):
                if adopted:
                    self._make_legacy_table()
                self._forwards()
                self._backwards()

                self.assertIn("adoptlegacy_widgets", _tables())
                self.assertNotIn("adopttest_widget", _tables())
                with connection.cursor() as cursor:
                    cursor.execute("DROP TABLE adoptlegacy_widgets")
                    cursor.execute(
                        "DELETE FROM django_content_type WHERE model = 'widget' AND app_label IN (%s, %s)",
                        [_APP, _LEGACY_APP],
                    )

    def test_an_uninstall_drops_the_table_instead(self) -> None:
        self._make_legacy_table()
        self._forwards()

        with dropping_adopted_tables():
            self._backwards()

        self.assertNotIn("adopttest_widget", _tables())
        self.assertNotIn("adoptlegacy_widgets", _tables())
