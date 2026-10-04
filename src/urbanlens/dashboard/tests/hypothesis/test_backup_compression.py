"""P202: a database backup is written gzip-compressed, and retention still counts the plain ones written before.

The first scheduled backup on the Kubernetes platform wrote 11.26 GB of plain SQL into a 12 Gi volume.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import mock

from urbanlens.core.controllers.backups.db import DatabaseBackup, is_backup_filename, is_backup_temp_filename
from urbanlens.core.tests.testcase import SimpleTestCase


def _backup(backup_dir: str | Path, *, retention: int = 5) -> DatabaseBackup:
    with mock.patch.object(DatabaseBackup, "schedule_backup", return_value=False):
        backup = DatabaseBackup(auto_schedule=False)
    backup.backup_dir = Path(backup_dir)
    backup.backup_retention = retention
    return backup


class CompressedDumpTests(SimpleTestCase):
    def _run(self, tmp: str) -> list[str]:
        def _dump(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"\x1f\x8b")
            return subprocess.CompletedProcess(cmd, 0)

        with (
            mock.patch("subprocess.run", side_effect=_dump) as run,
            mock.patch("urbanlens.core.controllers.backups.db.which", return_value="/usr/bin/pg_dump"),
        ):
            self.assertTrue(_backup(tmp).run())
        return run.call_args.args[0]

    def test_pg_dump_is_asked_for_gzip_compressed_sql(self) -> None:
        with TemporaryDirectory() as tmp:
            command = self._run(tmp)

        self.assertTrue(any(arg.startswith("--compress=gzip") for arg in command), command)

    def test_the_backup_is_named_for_its_compression(self) -> None:
        with TemporaryDirectory() as tmp:
            self._run(tmp)
            names = os.listdir(tmp)

        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".sql.gz"), names)


class BackupNamingTests(SimpleTestCase):
    def test_compressed_and_earlier_plain_backups_both_count(self) -> None:
        self.assertTrue(is_backup_filename("backup_20261004_010203.sql.gz"))
        self.assertTrue(is_backup_filename("backup_20261004_010203.sql"))
        self.assertFalse(is_backup_filename("backup_20261004_010203.sql.gz.bak"))

    def test_a_compressed_dump_in_progress_is_a_temp_file(self) -> None:
        self.assertTrue(is_backup_temp_filename("backup_20261004_010203.sql.gz.tmp"))

    def test_retention_counts_plain_and_compressed_backups_together(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [
                "backup_20261001_000000.sql",
                "backup_20261002_000000.sql",
                "backup_20261003_000000.sql.gz",
                "backup_20261004_000000.sql.gz",
            ]
            for index, name in enumerate(names):
                path = root / name
                path.write_bytes(b"x")
                os.utime(path, (1_000_000 + index, 1_000_000 + index))

            _backup(tmp, retention=2).purge_old_backups()

            self.assertEqual(sorted(os.listdir(tmp)), names[2:])
