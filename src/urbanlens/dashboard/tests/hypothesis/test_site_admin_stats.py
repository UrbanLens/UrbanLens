"""Tests for the site admin statistics page.

Covers:
- _monthly_series() label count, ordering, and accuracy
- _server_uptime() monotonic uptime formatting
- _dir_size_mb() size computation and error handling
- SiteAdminStatsView access control and context completeness
"""
from __future__ import annotations

from datetime import timedelta
import os
import pathlib
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.site_admin import (
    _dir_size_mb,
    _monthly_series,
    _server_uptime,
)
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.site_settings.meta import EnvironmentOverrideChoice
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group

# ── _monthly_series ───────────────────────────────────────────────────────────


class MonthlySeriesLabelTests(TestCase):
    """_monthly_series always returns exactly 12 labels in chronological order."""

    def test_returns_twelve_labels(self) -> None:
        labels, _ = _monthly_series(User.objects, "date_joined")
        self.assertEqual(len(labels), 12)

    def test_returns_twelve_counts(self) -> None:
        _, counts = _monthly_series(User.objects, "date_joined")
        self.assertEqual(len(counts), 12)

    def test_labels_are_strings(self) -> None:
        labels, _ = _monthly_series(User.objects, "date_joined")
        for label in labels:
            self.assertIsInstance(label, str)

    def test_counts_are_non_negative_ints(self) -> None:
        _, counts = _monthly_series(User.objects, "date_joined")
        for count in counts:
            self.assertIsInstance(count, int)
            self.assertGreaterEqual(count, 0)

    def test_empty_queryset_returns_all_zeros(self) -> None:
        # Use a fresh DB state - no users created yet.
        _, counts = _monthly_series(User.objects.none(), "date_joined")
        self.assertTrue(all(c == 0 for c in counts))

    def test_last_label_is_current_month(self) -> None:
        now = timezone.now()
        labels, _ = _monthly_series(User.objects, "date_joined")
        current_month = now.strftime("%b %Y")
        self.assertEqual(labels[-1], current_month)

    def test_counts_reflect_users_created_this_month(self) -> None:
        baker.make(User)
        baker.make(User)
        _, counts = _monthly_series(User.objects, "date_joined")
        # The last bucket covers this month; the two fresh users must appear.
        self.assertGreaterEqual(counts[-1], 2)

    def test_old_users_do_not_appear_in_current_month(self) -> None:
        # Create a user and backdate their join to 13 months ago (outside the 12-month window).
        old_user: User = baker.make(User)
        User.objects.filter(pk=old_user.pk).update(
            date_joined=timezone.now() - timedelta(days=400),
        )
        _, counts = _monthly_series(User.objects.filter(pk=old_user.pk), "date_joined")
        self.assertEqual(sum(counts), 0)


# ── _server_uptime ────────────────────────────────────────────────────────────


class ServerUptimeTests(TestCase):
    """_server_uptime reports app process uptime via the monotonic clock."""

    def _uptime_at(self, elapsed_seconds: float) -> str:
        """Return _server_uptime() when monotonic has advanced by ``elapsed_seconds``."""
        with mock.patch("urbanlens.dashboard.controllers.site_admin._APP_STARTED_MONOTONIC", 0.0), mock.patch(
            "urbanlens.dashboard.controllers.site_admin.time.monotonic",
            return_value=elapsed_seconds,
        ):
            return _server_uptime()

    def test_parses_days_hours_minutes_correctly(self) -> None:
        # 1 day + 2 hours + 3 minutes = 86400 + 7200 + 180 = 93780 seconds
        seconds = 86400 + 7200 + 180
        self.assertEqual(self._uptime_at(seconds), "1d 2h 3m")

    def test_zero_seconds_returns_zero_string(self) -> None:
        self.assertEqual(self._uptime_at(0), "0d 0h 0m")

    def test_exactly_one_hour(self) -> None:
        self.assertEqual(self._uptime_at(3600), "0d 1h 0m")

    def test_never_returns_negative_uptime(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.site_admin._APP_STARTED_MONOTONIC", 100.0), mock.patch(
            "urbanlens.dashboard.controllers.site_admin.time.monotonic",
            return_value=50.0,
        ):
            result = _server_uptime()
        self.assertEqual(result, "0d 0h 0m")


# ── _dir_size_mb ──────────────────────────────────────────────────────────────


class DirSizeMbTests(TestCase):
    """_dir_size_mb returns megabytes and handles missing paths gracefully."""

    def test_nonexistent_path_returns_zero(self) -> None:
        result = _dir_size_mb("/no/such/path/exists/12345")
        self.assertEqual(result, 0.0)

    def test_empty_directory_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _dir_size_mb(tmpdir)
        self.assertEqual(result, 0.0)

    def test_single_file_size_is_correct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sample.bin")
            pathlib.Path(path).write_bytes(b"x" * 1_048_576)  # exactly 1 MiB
            result = _dir_size_mb(tmpdir)
        self.assertAlmostEqual(result, 1.0, places=1)

    def test_multiple_files_are_summed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                path = os.path.join(tmpdir, f"file{i}.bin")
                pathlib.Path(path).write_bytes(b"x" * 524_288)  # 0.5 MiB each → 1.5 MiB total
            result = _dir_size_mb(tmpdir)
        self.assertAlmostEqual(result, 1.5, places=1)

    def test_returns_float(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _dir_size_mb(tmpdir)
        self.assertIsInstance(result, float)


# ── SiteAdminStatsView access control ─────────────────────────────────────────


class SiteAdminStatsViewAccessTests(TestCase):
    """Stats page requires the view_site_admin permission."""

    def test_unauthenticated_user_is_redirected(self) -> None:
        client = Client()
        response = client.get(reverse("site_admin_stats"))
        self.assertEqual(response.status_code, 302)

    def test_regular_user_gets_403(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        user: User = baker.make(User)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("site_admin_stats"))
        self.assertEqual(response.status_code, 403)

    def test_site_admin_gets_200(self) -> None:
        user: User = baker.make(User)
        add_user_to_site_admin_group(user)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("site_admin_stats"))
        self.assertEqual(response.status_code, 200)

    def test_superuser_gets_200(self) -> None:
        user: User = baker.make(User, is_superuser=True)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("site_admin_stats"))
        self.assertEqual(response.status_code, 200)


class SiteAdminStatsViewContextTests(TestCase):
    """Stats page context must contain all expected keys."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        add_user_to_site_admin_group(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def _get_context(self) -> dict:
        response = self.client.get(reverse("site_admin_stats"))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_context_has_total_users(self) -> None:
        ctx = self._get_context()
        self.assertIn("total_users", ctx)
        self.assertIsInstance(ctx["total_users"], int)

    def test_context_has_total_locations(self) -> None:
        ctx = self._get_context()
        self.assertIn("total_locations", ctx)

    def test_context_has_total_pins(self) -> None:
        ctx = self._get_context()
        self.assertIn("total_pins", ctx)

    def test_context_has_total_photos(self) -> None:
        ctx = self._get_context()
        self.assertIn("total_photos", ctx)

    def test_context_has_total_friendships(self) -> None:
        ctx = self._get_context()
        self.assertIn("total_friendships", ctx)

    def test_context_has_chart_data(self) -> None:
        ctx = self._get_context()
        self.assertIn("chart_user_labels", ctx)
        self.assertIn("chart_user_counts", ctx)
        self.assertIn("chart_location_labels", ctx)
        self.assertIn("chart_location_counts", ctx)

    def test_context_has_server_info(self) -> None:
        ctx = self._get_context()
        self.assertIn("python_version", ctx)
        self.assertIn("django_version", ctx)
        self.assertIn("server_time", ctx)

    def test_context_has_app_software_info(self) -> None:
        ctx = self._get_context()
        self.assertIn("app_version", ctx)
        self.assertIn("deployed_commit_short", ctx)
        self.assertIn("git_branch", ctx)
        self.assertIn("git_has_newer_commits", ctx)
        self.assertIn("git_available", ctx)
        self.assertIsInstance(ctx["app_version"], str)
        self.assertTrue(ctx["app_version"])

    def test_total_users_count_is_accurate(self) -> None:
        baker.make(User)
        baker.make(User)
        ctx = self._get_context()
        # At least our setUp user + the two we just created.
        self.assertGreaterEqual(ctx["total_users"], 3)

    def test_new_users_30d_is_non_negative(self) -> None:
        ctx = self._get_context()
        self.assertGreaterEqual(ctx["new_users_30d"], 0)

    def test_avg_pins_per_user_is_non_negative(self) -> None:
        ctx = self._get_context()
        self.assertGreaterEqual(ctx["avg_pins_per_user"], 0)


class SiteAdminPullLatestCodeViewTests(TestCase):
    """Code pulling is available only for site admins in development."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        add_user_to_site_admin_group(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_non_development_environment_returns_graceful_json_error(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.environment_override = EnvironmentOverrideChoice.PRODUCTION
        site_settings.save(update_fields=["environment_override"])

        response = self.client.post(reverse("site_admin_pull_latest_code"))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["ok"])

    def test_development_environment_pulls_and_reports_success(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.environment_override = EnvironmentOverrideChoice.DEVELOPMENT
        site_settings.save(update_fields=["environment_override"])

        with (
            mock.patch("urbanlens.core.version.get_current_git_commit", side_effect=["abc", "def"]),
            mock.patch("urbanlens.core.version.pull_latest_git_code", return_value=(True, "Updated")) as pull,
            mock.patch("urbanlens.core.version.apply_pending_migrations", return_value=(True, "Migrated")) as migrate,
            mock.patch(
                "urbanlens.core.version.trigger_development_app_reload",
                return_value=(True, "Reloading"),
            ) as reload_app,
        ):
            response = self.client.post(reverse("site_admin_pull_latest_code"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["changed"])
        self.assertEqual(response.json()["migration_details"], "Migrated")
        self.assertEqual(response.json()["reload_details"], "Reloading")
        pull.assert_called_once_with()
        migrate.assert_called_once_with()
        reload_app.assert_called_once_with()

    def test_development_environment_does_not_migrate_or_reload_without_code_change(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.environment_override = EnvironmentOverrideChoice.DEVELOPMENT
        site_settings.save(update_fields=["environment_override"])

        with (
            mock.patch("urbanlens.core.version.get_current_git_commit", side_effect=["abc", "abc"]),
            mock.patch("urbanlens.core.version.pull_latest_git_code", return_value=(True, "Already up to date")),
            mock.patch("urbanlens.core.version.apply_pending_migrations") as migrate,
            mock.patch("urbanlens.core.version.trigger_development_app_reload") as reload_app,
        ):
            response = self.client.post(reverse("site_admin_pull_latest_code"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(response.json()["changed"])
        migrate.assert_not_called()
        reload_app.assert_not_called()

    def test_development_environment_reports_migration_failure(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.environment_override = EnvironmentOverrideChoice.DEVELOPMENT
        site_settings.save(update_fields=["environment_override"])

        with (
            mock.patch("urbanlens.core.version.get_current_git_commit", side_effect=["abc", "def"]),
            mock.patch("urbanlens.core.version.pull_latest_git_code", return_value=(True, "Updated")),
            mock.patch("urbanlens.core.version.apply_pending_migrations", return_value=(False, "Migration failed")),
            mock.patch("urbanlens.core.version.trigger_development_app_reload") as reload_app,
        ):
            response = self.client.post(reverse("site_admin_pull_latest_code"))

        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Migration failed")
        reload_app.assert_not_called()
