"""A table no installed app owns, or a plugin's, cannot stop a core row being deleted: its foreign keys act in the database."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TransactionTestCase
from urbanlens.dashboard.plugins.integrity import ensure_foreign_keys_act_in_database

TABLE = "test_unowned_rows_about_users"


def _on_delete(table: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT confdeltype FROM pg_constraint WHERE contype = 'f' AND conrelid = %s::regclass", [table])
        return cursor.fetchone()[0]


class UnownedTableTests(TransactionTestCase):
    def setUp(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE {TABLE} (id bigserial PRIMARY KEY, user_id integer NOT NULL "
                "REFERENCES auth_user (id) DEFERRABLE INITIALLY DEFERRED)",
            )

    def tearDown(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {TABLE}")

    def test_an_unowned_table_cascades_once_rewritten_and_rewriting_is_idempotent(self) -> None:
        self.assertEqual(_on_delete(TABLE), "a")
        changes = ensure_foreign_keys_act_in_database()
        self.assertIn(TABLE, {change.table for change in changes})
        self.assertEqual(_on_delete(TABLE), "c")
        self.assertNotIn(TABLE, {change.table for change in ensure_foreign_keys_act_in_database()})

    def test_deleting_the_core_row_removes_the_unowned_rows(self) -> None:
        ensure_foreign_keys_act_in_database()
        user = baker.make(User)
        with connection.cursor() as cursor:
            cursor.execute(f"INSERT INTO {TABLE} (user_id) VALUES (%s)", [user.pk])
        user.delete()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {TABLE}")
            self.assertEqual(cursor.fetchone()[0], 0)
