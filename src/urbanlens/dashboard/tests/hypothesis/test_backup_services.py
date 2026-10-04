"""Tests for backup scheduling and statistics helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.backups import backup_files, collect_backup_stats, scheduled_backup_due


@dataclass(slots=True)
class _SiteSettings:
    backup_enabled: bool = True
    backup_frequency_hours: int = 24
    backup_retention: int = 30


def _touch(path: Path, when: datetime, size: int = 1) -> None:
    path.write_bytes(b"x" * size)
    timestamp = when.timestamp()
    os.utime(path, (timestamp, timestamp))


def _backup_name(when: datetime) -> str:
    """A filename in DatabaseBackup's own naming scheme for ``when``."""
    return f"backup_{when:%Y%m%d_%H%M%S}.sql"


class BackupFilesTests(SimpleTestCase):
    """backup_files returns existing files newest-first."""

    def test_returns_only_files_sorted_by_mtime_descending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 1, 1, tzinfo=UTC)
            old = root / _backup_name(base)
            new = root / _backup_name(base + timedelta(hours=1))
            (root / "nested").mkdir()
            # A stray non-backup file must be ignored rather than counted.
            (root / "notes.txt").write_bytes(b"x")
            _touch(old, base)
            _touch(new, base + timedelta(hours=1))

            self.assertEqual(backup_files(root), [new, old])

    def test_missing_directory_returns_empty_list(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp)
        self.assertEqual(backup_files(missing), [])

    def test_ignores_a_tmp_file_from_an_unfinished_dump(self) -> None:
        """A killed pg_dump's `.sql.tmp` almost matches the naming scheme but must
        never be counted alongside a completed backup."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 1, 1, tzinfo=UTC)
            done = root / _backup_name(base)
            partial = root / f"{_backup_name(base + timedelta(hours=1))}.tmp"
            _touch(done, base)
            _touch(partial, base + timedelta(hours=1))

            self.assertEqual(backup_files(root), [done])

    def test_uses_app_settings_backups_dir_when_none_given(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 1, 1, tzinfo=UTC)
            expected = root / _backup_name(base)
            _touch(expected, base)

            with mock.patch("urbanlens.dashboard.services.admin.backups.app_settings.backups_dir", root):
                self.assertEqual(backup_files(), [expected])


class ScheduledBackupDueTests(SimpleTestCase):
    """scheduled_backup_due respects enablement, frequency, and latest backup time."""

    def test_disabled_backups_are_never_due(self) -> None:
        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[]):
            self.assertFalse(scheduled_backup_due(_SiteSettings(backup_enabled=False)))

    def test_no_existing_backups_are_due(self) -> None:
        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[]):
            self.assertTrue(scheduled_backup_due(_SiteSettings()))

    @given(
        elapsed_hours=st.floats(min_value=0, max_value=240, allow_nan=False, allow_infinity=False),
        frequency_hours=st.integers(min_value=1, max_value=240),
    )
    @hyp_settings(max_examples=50)
    def test_due_when_elapsed_hours_meets_or_exceeds_frequency(
        self, elapsed_hours: float, frequency_hours: int
    ) -> None:
        now = datetime(2026, 1, 10, tzinfo=UTC)
        latest = now - timedelta(hours=elapsed_hours)
        fake_file = mock.Mock()
        fake_file.stat.return_value.st_mtime = latest.timestamp()

        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[fake_file]):
            due = scheduled_backup_due(_SiteSettings(backup_frequency_hours=frequency_hours), now=now)

        self.assertEqual(due, elapsed_hours >= frequency_hours)

    def test_due_exactly_at_frequency_boundary(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=UTC)
        frequency_hours = 24
        latest = now - timedelta(hours=frequency_hours)
        fake_file = mock.Mock()
        fake_file.stat.return_value.st_mtime = latest.timestamp()

        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[fake_file]):
            due = scheduled_backup_due(_SiteSettings(backup_frequency_hours=frequency_hours), now=now)

        # Equal-to-frequency elapsed time is exactly the ">=" cutoff. The property test
        # above almost never lands on this exact instant, so a ">" mutation of the
        # comparison would otherwise slip through - pin it explicitly.
        self.assertTrue(due)

    def test_not_due_one_second_before_frequency_boundary(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=UTC)
        frequency_hours = 24
        latest = now - timedelta(hours=frequency_hours) + timedelta(seconds=1)
        fake_file = mock.Mock()
        fake_file.stat.return_value.st_mtime = latest.timestamp()

        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[fake_file]):
            due = scheduled_backup_due(_SiteSettings(backup_frequency_hours=frequency_hours), now=now)

        self.assertFalse(due)


class CollectBackupStatsTests(SimpleTestCase):
    """collect_backup_stats summarizes backup directory contents."""

    def test_collects_count_latest_size_and_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 1, 1, tzinfo=UTC)
            _touch(root / _backup_name(base), base, size=1024)
            _touch(root / _backup_name(base + timedelta(hours=2)), base + timedelta(hours=2), size=2048)

            with (
                mock.patch("urbanlens.dashboard.services.admin.backups.app_settings.backups_dir", root),
                mock.patch(
                    "urbanlens.dashboard.services.admin.backups.backup_files",
                    wraps=lambda _backup_dir=None: backup_files(root),
                ),
            ):
                stats = collect_backup_stats(_SiteSettings(backup_frequency_hours=12, backup_retention=7))

        self.assertTrue(stats.enabled)
        self.assertEqual(stats.frequency_hours, 12)
        self.assertEqual(stats.retention, 7)
        self.assertEqual(stats.backup_dir, root)
        self.assertEqual(stats.count, 2)
        self.assertEqual(stats.latest_backup, base + timedelta(hours=2))
        # Exact value, not just "> 0" - a wrong divisor or a size computed from the
        # wrong files would still be positive and pass a mere greater-than check.
        self.assertAlmostEqual(stats.total_size_mb, (1024 + 2048) / 1_048_576)

    def test_collects_zero_count_and_none_latest_when_dir_is_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            with (
                mock.patch("urbanlens.dashboard.services.admin.backups.app_settings.backups_dir", root),
                mock.patch(
                    "urbanlens.dashboard.services.admin.backups.backup_files",
                    wraps=lambda _backup_dir=None: backup_files(root),
                ),
            ):
                stats = collect_backup_stats(_SiteSettings())

        self.assertEqual(stats.count, 0)
        self.assertIsNone(stats.latest_backup)
        self.assertEqual(stats.total_size_mb, 0.0)
        self.assertEqual(stats.backup_dir, root)

    def test_enabled_reflects_site_settings_toggle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            site_settings = _SiteSettings()

            with (
                mock.patch("urbanlens.dashboard.services.admin.backups.app_settings.backups_dir", root),
                mock.patch(
                    "urbanlens.dashboard.services.admin.backups.backup_files",
                    wraps=lambda _backup_dir=None: backup_files(root),
                ),
            ):
                self.assertTrue(collect_backup_stats(site_settings).enabled)
                site_settings.backup_enabled = False
                self.assertFalse(collect_backup_stats(site_settings).enabled)


class DefaultSiteSettingsTests(TestCase):
    """Both helpers resolve the live ``SiteSettings`` singleton when none is passed in.

    Every production caller (the site-admin view, the scheduled-backup Celery task, and ``DatabaseBackup``
    itself - see ``core.controllers.backups.db`` and ``dashboard.tasks``) omits the ``site_settings`` argument
    entirely."""

    def setUp(self) -> None:
        super().setUp()
        self.site_settings = SiteSettings.get_current()

    def test_scheduled_backup_due_follows_the_live_enabled_flag(self) -> None:
        self.site_settings.backup_enabled = False
        self.site_settings.save(update_fields=["backup_enabled", "updated"])

        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[]):
            self.assertFalse(scheduled_backup_due())

            self.site_settings.backup_enabled = True
            self.site_settings.save(update_fields=["backup_enabled", "updated"])

            self.assertTrue(scheduled_backup_due())

    def test_collect_backup_stats_reflects_the_live_settings_row(self) -> None:
        self.site_settings.backup_enabled = False
        self.site_settings.backup_frequency_hours = 6
        self.site_settings.backup_retention = 3
        self.site_settings.save(
            update_fields=["backup_enabled", "backup_frequency_hours", "backup_retention", "updated"]
        )

        with mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[]):
            stats = collect_backup_stats()

        self.assertFalse(stats.enabled)
        self.assertEqual(stats.frequency_hours, 6)
        self.assertEqual(stats.retention, 3)


class DeploymentSwitchTests(SimpleTestCase):
    """``UL_BACKUP_ENABLED=false`` turns scheduled backups off whatever the settings row says (P202).

    On Kubernetes the backups directory is an emptyDir that a new pod sees empty, so the task always reads as due, and
    a restored settings row brought it back on after infrastructure had switched it off.
    """

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        self.assertTrue(hasattr(app_settings, "backup_enabled"))
        self.app_settings = app_settings
        self.enterContext(mock.patch("urbanlens.dashboard.services.admin.backups.backup_files", return_value=[]))

    def test_the_deployment_switch_wins_over_the_settings_row(self) -> None:
        with mock.patch.object(self.app_settings, "backup_enabled", False):
            self.assertFalse(scheduled_backup_due(_SiteSettings(backup_enabled=True)))

    def test_the_settings_row_decides_when_the_deployment_allows_backups(self) -> None:
        with mock.patch.object(self.app_settings, "backup_enabled", True):
            self.assertTrue(scheduled_backup_due(_SiteSettings(backup_enabled=True)))
            self.assertFalse(scheduled_backup_due(_SiteSettings(backup_enabled=False)))

    def test_the_stats_say_the_deployment_turned_backups_off(self) -> None:
        with mock.patch.object(self.app_settings, "backup_enabled", False):
            stats = collect_backup_stats(_SiteSettings(backup_enabled=True))

        self.assertFalse(stats.enabled)
        self.assertTrue(stats.disabled_by_deployment)


class DeploymentSwitchAdminPageTests(TestCase):
    def test_the_site_admin_page_says_the_deployment_turned_backups_off(self) -> None:
        from django.urls import reverse
        from model_bakery import baker

        from urbanlens.UrbanLens.settings.app import settings as app_settings

        self.client.force_login(baker.make("auth.User", is_staff=True, is_superuser=True))
        with mock.patch.object(app_settings, "backup_enabled", False):
            response = self.client.get(reverse("site_admin"))

        self.assertContains(response, "UL_BACKUP_ENABLED=false")
        self.assertContains(response, 'name="backup_enabled" id="backup-enabled" disabled')

    def test_saving_the_form_while_the_deployment_disables_backups_keeps_the_rows_choice(self) -> None:
        """The disabled select is not posted, and its absence must not read as "off"."""
        from django.urls import reverse
        from model_bakery import baker

        from urbanlens.UrbanLens.settings.app import settings as app_settings

        site_settings = SiteSettings.get_current()
        site_settings.backup_enabled = True
        site_settings.save(update_fields=["backup_enabled", "updated"])
        self.client.force_login(baker.make("auth.User", is_staff=True, is_superuser=True))

        with mock.patch.object(app_settings, "backup_enabled", False):
            self.client.post(reverse("site_admin"), {"backup_frequency_hours": "12", "backup_retention": "4"})

        site_settings.refresh_from_db()
        self.assertTrue(site_settings.backup_enabled)
        self.assertEqual(site_settings.backup_frequency_hours, 12)
