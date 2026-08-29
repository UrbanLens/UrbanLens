"""A dump that dies mid-write leaves a `.tmp` behind that nothing used to reap.

`run()` writes to `<name>.sql.tmp` and renames only on success, so a partial dump can
never be mistaken for a complete backup. That is deliberate, but it means retention -
which only ever considers `is_backup_filename` matches - never counts or removes those
files. A process death mid-dump (OOM kill, container restart) is exactly the case the
rename guards against, and every occurrence left a full-dump-sized file on disk forever.

`purge_stale_temp_files` reaps them, but only once they are far too old to be a dump
still in progress - deleting a live dump's temp file would corrupt a running backup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import mock

from django.conf import settings as django_settings

from urbanlens.core.controllers.backups.db import BACKUP_TIMEOUT_SECONDS, STALE_TEMP_AGE_SECONDS, DatabaseBackup, is_backup_temp_filename
from urbanlens.core.tests.testcase import SimpleTestCase


def _touch(path: Path, age_seconds: float) -> None:
    """Create a file whose mtime is ``age_seconds`` in the past."""
    path.write_bytes(b"x")
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).timestamp()
    os.utime(path, (stamp, stamp))


class BackupTempFilenameTests(SimpleTestCase):
    def test_only_this_class_s_own_temp_files_match(self) -> None:
        self.assertTrue(is_backup_temp_filename("backup_20260811_120000.sql.tmp"))
        # A stray temp file from anything else must never be deleted by the reaper.
        self.assertFalse(is_backup_temp_filename("something-else.tmp"))
        self.assertFalse(is_backup_temp_filename("backup_bogus.sql.tmp"))
        # A completed backup is retention's business, not the reaper's.
        self.assertFalse(is_backup_temp_filename("backup_20260811_120000.sql"))


class PurgeStaleTempFilesTests(SimpleTestCase):
    def _backup(self, backup_dir: str | Path) -> DatabaseBackup:
        with mock.patch.object(DatabaseBackup, "schedule_backup", return_value=False):
            backup = DatabaseBackup(auto_schedule=False)
        backup.backup_dir = Path(backup_dir)
        backup.backup_retention = 5
        return backup

    def test_a_long_abandoned_temp_file_is_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            stale = Path(tmp) / "backup_20260101_000000.sql.tmp"
            _touch(stale, STALE_TEMP_AGE_SECONDS + 60)

            self._backup(tmp).purge_stale_temp_files()

            self.assertFalse(stale.exists())

    def test_an_in_flight_dump_is_left_alone(self) -> None:
        """The regression that would matter most: reaping a live dump's temp file
        corrupts the backup currently being written."""
        with TemporaryDirectory() as tmp:
            live = Path(tmp) / "backup_20260811_120000.sql.tmp"
            _touch(live, 30)

            self._backup(tmp).purge_stale_temp_files()

            self.assertTrue(live.exists())

    def test_the_stale_cutoff_boundary_is_pinned_to_the_second(self) -> None:
        """`> cutoff` (not `>=`) decides which side of STALE_TEMP_AGE_SECONDS a file falls
        on - pin the exact cutoff instant and one second inside it, not just values
        comfortably on either side, so a flipped comparison or an off-by-one survives no
        longer than this test."""
        with TemporaryDirectory() as tmp:
            fixed_now = 10_000_000.0
            cutoff = fixed_now - STALE_TEMP_AGE_SECONDS

            at_cutoff = Path(tmp) / "backup_20260101_000000.sql.tmp"
            at_cutoff.write_bytes(b"x")
            os.utime(at_cutoff, (cutoff, cutoff))

            one_second_short = Path(tmp) / "backup_20260102_000000.sql.tmp"
            one_second_short.write_bytes(b"x")
            os.utime(one_second_short, (cutoff + 1, cutoff + 1))

            with mock.patch("urbanlens.core.controllers.backups.db.datetime") as mock_dt:
                mock_dt.now.return_value.timestamp.return_value = fixed_now
                self._backup(tmp).purge_stale_temp_files()

            self.assertFalse(at_cutoff.exists(), "a file exactly at the cutoff must be reaped")
            self.assertTrue(one_second_short.exists(), "a file one second inside the cutoff must survive")

    def test_completed_backups_are_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            done = Path(tmp) / "backup_20260101_000000.sql"
            _touch(done, STALE_TEMP_AGE_SECONDS + 60)

            self._backup(tmp).purge_stale_temp_files()

            self.assertTrue(done.exists())

    def test_unrelated_stale_files_are_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            stray = Path(tmp) / "somebody-elses.tmp"
            _touch(stray, STALE_TEMP_AGE_SECONDS + 60)

            self._backup(tmp).purge_stale_temp_files()

            self.assertTrue(stray.exists())

    def test_a_removal_failure_does_not_abort_the_rest_of_the_sweep(self) -> None:
        """The try/except around os.remove exists so one uncooperative file (a permission
        error, a concurrent delete) can't abort the sweep before later stale files are
        reaped - assert the loop actually continues rather than propagating."""
        with TemporaryDirectory() as tmp:
            unremovable = Path(tmp) / "backup_20260101_000000.sql.tmp"
            removable = Path(tmp) / "backup_20260102_000000.sql.tmp"
            _touch(unremovable, STALE_TEMP_AGE_SECONDS + 60)
            _touch(removable, STALE_TEMP_AGE_SECONDS + 60)

            real_remove = os.remove

            def _flaky_remove(path, *args, **kwargs) -> None:
                if os.fspath(path) == str(unremovable):
                    raise OSError("permission denied")
                real_remove(path, *args, **kwargs)

            with mock.patch("os.remove", side_effect=_flaky_remove):
                self._backup(tmp).purge_stale_temp_files()

            self.assertTrue(unremovable.exists())
            self.assertFalse(removable.exists())

    def test_purging_old_backups_also_reaps_temp_files(self) -> None:
        """The reaper has no scheduler of its own - it rides along with retention,
        which runs after every successful dump."""
        with TemporaryDirectory() as tmp:
            stale = Path(tmp) / "backup_20260101_000000.sql.tmp"
            _touch(stale, STALE_TEMP_AGE_SECONDS + 60)

            self._backup(tmp).purge_old_backups()

            self.assertFalse(stale.exists())


class BackupTimeoutTests(SimpleTestCase):
    """A wedged pg_dump must fail cleanly rather than run to the Celery task limit.

    `subprocess.TimeoutExpired` is not a `CalledProcessError`, so the original handler
    would not have caught one had a timeout been passed - it would propagate out of the
    task leaving the partial `.tmp` behind.
    """

    def _backup(self, backup_dir: str | Path) -> DatabaseBackup:
        backup = DatabaseBackup(auto_schedule=False)
        backup.backup_dir = Path(backup_dir)
        backup.backup_retention = 5
        return backup

    def test_a_wedged_dump_returns_false_and_leaves_no_temp_file(self) -> None:
        with TemporaryDirectory() as tmp:
            backup = self._backup(tmp)

            def _hang(cmd, **kwargs):
                # pg_dump got as far as creating its output file, then wedged.
                Path(cmd[cmd.index("-f") + 1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

            # `db.py` does `from shutil import which`, so the name to patch is its own.
            with mock.patch("subprocess.run", side_effect=_hang) as mock_run, mock.patch("urbanlens.core.controllers.backups.db.which", return_value="/usr/bin/pg_dump"):
                self.assertFalse(backup.run())

            # A dropped `timeout=` kwarg would restore the original bug (the wedge running
            # until Celery's own soft limit) while `_hang` above still raises regardless -
            # pin that the argument is actually threaded through, not just that some
            # TimeoutExpired gets raised somewhere.
            self.assertEqual(mock_run.call_args.kwargs.get("timeout"), BACKUP_TIMEOUT_SECONDS)
            self.assertEqual(list(Path(tmp).iterdir()), [], "the partial dump was left on disk")

    def test_the_timeout_is_below_the_celery_soft_limit(self) -> None:
        """Otherwise the task limit fires first and the cleanup above never runs."""
        self.assertLess(BACKUP_TIMEOUT_SECONDS, django_settings.CELERY_TASK_SOFT_TIME_LIMIT)
