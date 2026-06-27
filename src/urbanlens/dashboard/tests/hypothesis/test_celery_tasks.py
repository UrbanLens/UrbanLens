"""Unit tests for dashboard Celery task bodies."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks


class CreateLocationForPinTaskTests(TestCase):
    """create_location_for_pin delegates to the location service and reports progress."""

    def test_returns_created_location_id(self) -> None:
        fake_location = mock.Mock(pk=123)
        with (
            mock.patch("urbanlens.dashboard.tasks.LocationCreationService") as service,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            service.return_value.create_for_pin.return_value = fake_location
            result = tasks.create_location_for_pin(99)

        self.assertEqual(result, 123)
        service.return_value.create_for_pin.assert_called_once_with(99)
        self.assertEqual(progress.call_count, 2)


class DatabaseBackupTaskTests(TestCase):
    """Database backup tasks use site settings and scheduled due checks."""

    def test_run_database_backup_uses_site_settings_retention(self) -> None:
        task = mock.Mock()
        fake_backup = mock.Mock()
        fake_backup.run.return_value = True
        fake_site_settings = mock.Mock(backup_retention=5)

        with (
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup", return_value=fake_backup) as backup_cls,
            mock.patch("urbanlens.dashboard.models.site_settings.SiteSettings.get_current", return_value=fake_site_settings),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks._run_database_backup(task)

        self.assertTrue(result)
        backup_cls.assert_called_once_with(auto_schedule=False)
        self.assertEqual(fake_backup.backup_retention, 5)
        fake_backup.create_backup_dir.assert_called_once_with()
        fake_backup.run.assert_called_once_with()
        self.assertEqual(progress.call_count, 2)

    def test_scheduled_backup_skips_when_not_due(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.backups.scheduled_backup_due", return_value=False),
            mock.patch("urbanlens.dashboard.tasks._run_database_backup") as backup,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks.run_scheduled_database_backup()

        self.assertFalse(result)
        backup.assert_not_called()
        progress.assert_called_once()

    def test_scheduled_backup_runs_when_due(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.backups.scheduled_backup_due", return_value=True),
            mock.patch("urbanlens.dashboard.tasks._run_database_backup", return_value=True) as backup,
        ):
            result = tasks.run_scheduled_database_backup()

        self.assertTrue(result)
        backup.assert_called_once()
