"""Tests for backup scheduling and statistics helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.backups import backup_files, collect_backup_stats, scheduled_backup_due


@dataclass(slots=True)
class _SiteSettings:
    backup_enabled: bool = True
    backup_frequency_hours: int = 24
    backup_retention: int = 30


def _touch(path: Path, when: datetime, size: int = 1) -> None:
    path.write_bytes(b"x" * size)
    timestamp = when.timestamp()
    os.utime(path, (timestamp, timestamp))


class BackupFilesTests(TestCase):
    """backup_files returns existing files newest-first."""

    def test_returns_only_files_sorted_by_mtime_descending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.sql"
            new = root / "new.sql"
            (root / "nested").mkdir()
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            _touch(old, base)
            _touch(new, base + timedelta(hours=1))

            self.assertEqual(backup_files(root), [new, old])

    def test_missing_directory_returns_empty_list(self) -> None:
        self.assertEqual(backup_files(Path("/tmp/urbanlens-missing-backups-for-test")), [])


class ScheduledBackupDueTests(TestCase):
    """scheduled_backup_due respects enablement, frequency, and latest backup time."""

    def test_disabled_backups_are_never_due(self) -> None:
        with mock.patch("urbanlens.dashboard.services.backups.backup_files", return_value=[]):
            self.assertFalse(scheduled_backup_due(_SiteSettings(backup_enabled=False)))

    def test_no_existing_backups_are_due(self) -> None:
        with mock.patch("urbanlens.dashboard.services.backups.backup_files", return_value=[]):
            self.assertTrue(scheduled_backup_due(_SiteSettings()))

    @given(
        elapsed_hours=st.floats(min_value=0, max_value=240, allow_nan=False, allow_infinity=False),
        frequency_hours=st.integers(min_value=1, max_value=240),
    )
    @hyp_settings(max_examples=50)
    def test_due_when_elapsed_hours_meets_or_exceeds_frequency(self, elapsed_hours: float, frequency_hours: int) -> None:
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        latest = now - timedelta(hours=elapsed_hours)
        fake_file = mock.Mock()
        fake_file.stat.return_value.st_mtime = latest.timestamp()

        with mock.patch("urbanlens.dashboard.services.backups.backup_files", return_value=[fake_file]):
            due = scheduled_backup_due(_SiteSettings(backup_frequency_hours=frequency_hours), now=now)

        self.assertEqual(due, elapsed_hours >= frequency_hours)


class CollectBackupStatsTests(TestCase):
    """collect_backup_stats summarizes backup directory contents."""

    def test_collects_count_latest_size_and_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            _touch(root / "a.sql", base, size=1024)
            _touch(root / "b.sql", base + timedelta(hours=2), size=2048)

            with (
                mock.patch("urbanlens.dashboard.services.backups.app_settings.backups_dir", root),
                mock.patch("urbanlens.dashboard.services.backups.backup_files", wraps=lambda backup_dir=None: backup_files(root)),
            ):
                stats = collect_backup_stats(_SiteSettings(backup_frequency_hours=12, backup_retention=7))

        self.assertTrue(stats.enabled)
        self.assertEqual(stats.frequency_hours, 12)
        self.assertEqual(stats.retention, 7)
        self.assertEqual(stats.count, 2)
        self.assertEqual(stats.latest_backup, base + timedelta(hours=2))
        self.assertGreater(stats.total_size_mb, 0)
