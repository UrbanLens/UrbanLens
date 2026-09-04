"""Unit tests for dashboard Celery task bodies."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks


class GenerateBoundariesForLocationTaskTests(TestCase):
    """generate_boundaries_for_location runs the provider chain once per Location."""

    def test_missing_location_is_a_noop(self) -> None:
        with mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate:
            result = tasks.generate_boundaries_for_location(999999)

        self.assertFalse(result)
        generate.assert_not_called()

    def test_skips_when_generation_already_ran_and_still_fresh(self) -> None:
        from model_bakery import baker

        location = baker.make_recipe("dashboard.location")
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.generation_status", return_value=(True, False)
            ),
            mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate,
        ):
            result = tasks.generate_boundaries_for_location(location.pk)

        self.assertTrue(result)
        generate.assert_not_called()

    def test_regenerates_when_already_ran_but_stale(self) -> None:
        """A stale row still gets refreshed - only a fresh one is skipped."""
        from model_bakery import baker

        location = baker.make_recipe("dashboard.location")
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.generation_status", return_value=(True, True)
            ),
            mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate,
        ):
            result = tasks.generate_boundaries_for_location(location.pk)

        self.assertTrue(result)
        generate.assert_called_once_with(location)

    def test_regenerates_when_never_run_even_if_not_marked_stale(self) -> None:
        """The "not ran" arm of the ``or`` must trigger generation on its own -
        a first-ever generation has nothing to be stale relative to."""
        from model_bakery import baker

        location = baker.make_recipe("dashboard.location")
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.generation_status", return_value=(False, False)
            ),
            mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate,
        ):
            result = tasks.generate_boundaries_for_location(location.pk)

        self.assertTrue(result)
        generate.assert_called_once_with(location)


class PushTripToCalendarTaskTests(TestCase):
    """push_trip_to_calendar looks up the trip and delegates to the sync service."""

    def test_missing_trip_is_a_noop(self) -> None:
        with mock.patch("urbanlens.dashboard.services.trips.calendar_sync.push_auto_synced_trip_changes") as push:
            result = tasks.push_trip_to_calendar(999999)

        self.assertEqual(result, 0)
        push.assert_not_called()

    def test_existing_trip_is_pushed(self) -> None:
        from model_bakery import baker

        trip = baker.make("dashboard.Trip")
        with mock.patch(
            "urbanlens.dashboard.services.trips.calendar_sync.push_auto_synced_trip_changes", return_value=2
        ) as push:
            result = tasks.push_trip_to_calendar(trip.pk)

        self.assertEqual(result, 2)
        push.assert_called_once_with(trip)


class DatabaseBackupTaskTests(SimpleTestCase):
    """Database backup tasks use site settings and scheduled due checks."""

    def test_run_database_backup_uses_site_settings_retention(self) -> None:
        task = mock.Mock()
        fake_backup = mock.Mock()
        fake_backup.run.return_value = True
        fake_site_settings = mock.Mock(backup_retention=5)

        with (
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup", return_value=fake_backup) as backup_cls,
            mock.patch(
                "urbanlens.dashboard.models.site_settings.SiteSettings.get_current", return_value=fake_site_settings
            ),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks._run_database_backup(task)

        self.assertTrue(result)
        backup_cls.assert_called_once_with(auto_schedule=False)
        self.assertEqual(fake_backup.backup_retention, 5)
        fake_backup.create_backup_dir.assert_called_once_with()
        fake_backup.run.assert_called_once_with()
        self.assertEqual(progress.call_count, 2)
        progress.assert_called_with(task, current=1, total=1, message="Database backup complete")

    def test_run_database_backup_reports_failure_when_backup_run_fails(self) -> None:
        """The final progress message and return value both track ``backup.run()`` -
        pinned separately from the success case so a hardcoded True can't hide behind it."""
        task = mock.Mock()
        fake_backup = mock.Mock()
        fake_backup.run.return_value = False
        fake_site_settings = mock.Mock(backup_retention=5)

        with (
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup", return_value=fake_backup),
            mock.patch(
                "urbanlens.dashboard.models.site_settings.SiteSettings.get_current", return_value=fake_site_settings
            ),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks._run_database_backup(task)

        self.assertFalse(result)
        progress.assert_called_with(task, current=1, total=1, message="Database backup failed")

    def test_run_database_backup_works_without_a_task_handle(self) -> None:
        """task defaults to None (an ad-hoc/management-command call, not a Celery
        worker) and must skip progress reporting entirely rather than call it with None."""
        fake_backup = mock.Mock()
        fake_backup.run.return_value = True
        fake_site_settings = mock.Mock(backup_retention=5)

        with (
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup", return_value=fake_backup),
            mock.patch(
                "urbanlens.dashboard.models.site_settings.SiteSettings.get_current", return_value=fake_site_settings
            ),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks._run_database_backup()

        self.assertTrue(result)
        progress.assert_not_called()

    def test_scheduled_backup_skips_when_not_due(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.admin.backups.scheduled_backup_due", return_value=False),
            mock.patch("urbanlens.dashboard.tasks._run_database_backup") as backup,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks.run_scheduled_database_backup()

        self.assertFalse(result)
        backup.assert_not_called()
        progress.assert_called_once()

    def test_scheduled_backup_runs_when_due(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.admin.backups.scheduled_backup_due", return_value=True),
            mock.patch("urbanlens.dashboard.tasks._run_database_backup", return_value=True) as backup,
        ):
            result = tasks.run_scheduled_database_backup()

        self.assertTrue(result)
        backup.assert_called_once()


class AdvancePwywUsageLedgersTaskTests(TestCase):
    """advance_pwyw_usage_ledgers is the daily safety net that keeps a canceled
    pay-what-you-want subscription's banked balance counting down - invoice.payment_succeeded
    is the only other trigger, and it stops firing once Stripe considers the subscription gone."""

    def test_ticks_every_pwyw_subscription_and_ignores_fixed_price_roles(self) -> None:
        from django.contrib.auth.models import User
        from model_bakery import baker

        from urbanlens.dashboard.models.billing import RoleSubscription
        from urbanlens.dashboard.models.subscriptions import SubscriptionRole

        pwyw_role = baker.make(SubscriptionRole, pay_what_you_want=True)
        fixed_role = baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=500)
        user = baker.make(User)
        pwyw_sub = baker.make(RoleSubscription, user=user, role=pwyw_role)
        baker.make(RoleSubscription, user=user, role=fixed_role)

        with mock.patch("urbanlens.dashboard.services.billing.banking.advance_usage_ledger") as advance:
            count = tasks.advance_pwyw_usage_ledgers()

        self.assertEqual(count, 1)
        advance.assert_called_once_with(pwyw_sub)

    def test_one_failing_subscription_does_not_block_the_rest(self) -> None:
        """The per-row try/except must swallow a raised error and keep sweeping - one
        broken subscription can't be allowed to freeze every other user's ledger."""
        from django.contrib.auth.models import User
        from model_bakery import baker

        from urbanlens.dashboard.models.billing import RoleSubscription
        from urbanlens.dashboard.models.subscriptions import SubscriptionRole

        pwyw_role = baker.make(SubscriptionRole, pay_what_you_want=True)
        failing_sub = baker.make(RoleSubscription, user=baker.make(User), role=pwyw_role)
        ok_sub = baker.make(RoleSubscription, user=baker.make(User), role=pwyw_role)

        def side_effect(role_subscription: RoleSubscription) -> None:
            if role_subscription.pk == failing_sub.pk:
                raise RuntimeError("boom")

        with mock.patch(
            "urbanlens.dashboard.services.billing.banking.advance_usage_ledger", side_effect=side_effect
        ) as advance:
            count = tasks.advance_pwyw_usage_ledgers()

        self.assertEqual(count, 1)
        self.assertEqual(advance.call_count, 2)
        advance.assert_any_call(ok_sub)

    def test_exhausts_a_canceled_subscriptions_balance_over_time(self) -> None:
        from datetime import timedelta

        from django.contrib.auth.models import User
        from django.utils import timezone
        from model_bakery import baker

        from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription
        from urbanlens.dashboard.models.subscriptions import SubscriptionRole

        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        sub = baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=1000,
        )
        RoleSubscription.objects.filter(pk=sub.pk).update(created=timezone.now() - timedelta(days=200))

        tasks.advance_pwyw_usage_ledgers()

        sub.refresh_from_db()
        self.assertFalse(sub.has_banked_access)
        self.assertEqual(sub.amount_used_cents, 1000)
