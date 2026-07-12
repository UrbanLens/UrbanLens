"""Unit tests for dashboard Celery task bodies."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks


class GenerateBoundariesForLocationTaskTests(TestCase):
    """generate_boundaries_for_location runs the provider chain once per Location."""

    def test_missing_location_is_a_noop(self) -> None:
        with mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate:
            result = tasks.generate_boundaries_for_location(999999)

        self.assertFalse(result)
        generate.assert_not_called()

    def test_skips_when_generation_already_ran(self) -> None:
        from model_bakery import baker

        location = baker.make_recipe("dashboard.location")
        with (
            mock.patch("urbanlens.dashboard.services.locations.boundaries.boundary_generation_ran", return_value=True),
            mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate,
        ):
            result = tasks.generate_boundaries_for_location(location.pk)

        self.assertTrue(result)
        generate.assert_not_called()


class PushTripToCalendarTaskTests(TestCase):
    """push_trip_to_calendar looks up the trip and delegates to the sync service."""

    def test_missing_trip_is_a_noop(self) -> None:
        with mock.patch("urbanlens.dashboard.services.calendar_sync.push_auto_synced_trip_changes") as push:
            result = tasks.push_trip_to_calendar(999999)

        self.assertEqual(result, 0)
        push.assert_not_called()

    def test_existing_trip_is_pushed(self) -> None:
        from model_bakery import baker

        trip = baker.make("dashboard.Trip")
        with mock.patch("urbanlens.dashboard.services.calendar_sync.push_auto_synced_trip_changes", return_value=2) as push:
            result = tasks.push_trip_to_calendar(trip.pk)

        self.assertEqual(result, 2)
        push.assert_called_once_with(trip)


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
