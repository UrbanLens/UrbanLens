"""The held-upload indexes are built outside a transaction, so a deploy interrupted part-way must be able to run again.

Django records a non-atomic migration only once every operation has finished. An interrupted run leaves the indexes it
built (and, for a concurrent build cancelled part-way, an invalid one under the same name), and the next deploy's
``migrate`` runs every operation again.
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from urbanlens.dashboard.models.pin.model import Pin

_BEFORE = ("dashboard", "0042_held_icon_and_avatar_uploads")
_AFTER = ("dashboard", "0043_held_upload_partial_indexes")
_INDEXES = ("idxdb_pin_held_icon", "idxdb_label_held_icon", "idxdb_achv_held_icon", "idxdb_profile_held_avatar")


def _migrate(target: tuple[str, str]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])


class TheHeldUploadIndexMigrationTests(TransactionTestCase):
    def _restore(self) -> None:
        if _AFTER in MigrationRecorder(connection).applied_migrations():
            return
        with connection.cursor() as cursor:
            for name in _INDEXES:
                cursor.execute(f'DROP INDEX IF EXISTS "{name}"')
        _migrate(_AFTER)

    def test_it_runs_again_after_a_deploy_interrupted_part_way(self) -> None:
        _migrate(_BEFORE)
        self.addCleanup(self._restore)
        table = Pin._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE INDEX "idxdb_pin_held_icon" ON "{table}" ("custom_icon_upload")')

        _migrate(_AFTER)

        with connection.cursor() as cursor:
            cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE indexname = 'idxdb_pin_held_icon'")
            (_name, definition), *_ = cursor.fetchall()
        self.assertIn("WHERE", definition, "the index left by the interrupted run was kept instead of the partial one")
