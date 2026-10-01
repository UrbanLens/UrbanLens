"""Tests for the post_save signals that push auto-synced trips to Google Calendar."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.trips.signals import sync_trip_on_activity_save, sync_trip_on_save


class TripCalendarAutoSyncSignalTests(TestCase):
    """Trip/TripActivity saves enqueue a calendar push only when auto-sync is on."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="auto-sync-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.trip = Trip.objects.create(
            name="Signal trip",
            creator=self.profile,
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 11, 2),
        )

    def _enqueue_for_trip_save(self):
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            sync_trip_on_save(sender=Trip, instance=self.trip)
            for callback in callbacks:
                callback()
        return enqueue

    def test_trip_without_auto_sync_link_does_not_enqueue(self):
        enqueue = self._enqueue_for_trip_save()
        enqueue.assert_not_called()

    def test_trip_with_auto_sync_link_enqueues_push(self):
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-1",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
        )

        enqueue = self._enqueue_for_trip_save()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.trip.pk)

    def test_trip_with_manual_export_link_does_not_enqueue(self):
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-1",
            direction=CalendarSyncDirection.EXPORTED,
            auto_sync=False,
        )

        enqueue = self._enqueue_for_trip_save()
        enqueue.assert_not_called()

    def test_activity_save_enqueues_push_for_its_trip_when_auto_synced(self):
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-1",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
        )
        activity = TripActivity.objects.create(trip=self.trip, title="New stop")

        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            sync_trip_on_activity_save(sender=TripActivity, instance=activity)
            for callback in callbacks:
                callback()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.trip.pk)

    def test_activity_link_scoped_to_a_different_trip_does_not_enqueue(self):
        """An activity-level link's auto_sync flag must not leak into the trip-level check."""
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-activity-only",
            direction=CalendarSyncDirection.EXPORTED,
            activity=TripActivity.objects.create(trip=self.trip, title="Other activity"),
            auto_sync=True,
        )

        enqueue = self._enqueue_for_trip_save()
        enqueue.assert_not_called()


class CalendarPushRequestTests(TestCase):
    """An auto-sync request stays owed until a push delivers it, and a sweep retries it."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="push-request-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.trip = Trip.objects.create(
            name="Request trip",
            creator=self.profile,
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 11, 2),
        )
        self.link = TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-1",
            direction=CalendarSyncDirection.EXPORTED,
            auto_sync=True,
        )

    def _request(self):
        from urbanlens.dashboard.models.trips.signals import queue_calendar_push

        with mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit"):
            queue_calendar_push(self.trip.pk)
        self.link.refresh_from_db()
        return self.link.push_requested_at

    def _push(self, **export):
        from urbanlens.dashboard.services.trips import calendar_sync

        with mock.patch.object(calendar_sync, "export_trip_to_calendar", **export):
            return calendar_sync.push_auto_synced_trip_changes(self.trip)

    def test_a_trip_save_marks_the_link_as_owing_a_push(self):
        self.assertIsNotNone(self._request())

    def test_a_delivered_push_clears_the_request(self):
        self._request()

        self.assertEqual(self._push(), 1)

        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)

    def test_a_failed_push_keeps_the_request_and_counts_the_attempt(self):
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        self._request()

        self.assertEqual(self._push(side_effect=GatewayRequestError("503")), 0)

        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.push_requested_at)
        self.assertEqual(self.link.push_attempts, 1)

    def test_a_change_made_during_the_push_stays_owed(self):
        self._request()
        later = timezone.now() + datetime.timedelta(seconds=5)

        def change_meanwhile(*_args, **_kwargs):
            TripCalendarLink.objects.filter(pk=self.link.pk).update(push_requested_at=later)

        self._push(side_effect=change_meanwhile)

        self.link.refresh_from_db()
        self.assertEqual(self.link.push_requested_at, later)

    def test_a_revoked_grant_settles_the_request(self):
        from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError

        self._request()

        self._push(side_effect=GoogleAuthExpiredError("revoked"))

        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)

    def _age_request(self, *, attempts=0):
        stale = timezone.now() - datetime.timedelta(hours=1)
        TripCalendarLink.objects.filter(pk=self.link.pk).update(push_requested_at=stale, push_attempts=attempts)

    def test_the_sweep_queues_a_stale_request(self):
        from urbanlens.dashboard.tasks import push_trip_to_calendar, requeue_pending_calendar_pushes

        self._age_request()

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_pending_calendar_pushes(), 1)

        enqueue.assert_called_once_with(push_trip_to_calendar, self.trip.pk, durable=False)

    def test_the_sweep_leaves_a_fresh_request_to_its_own_push(self):
        from urbanlens.dashboard.tasks import requeue_pending_calendar_pushes

        self._request()

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_pending_calendar_pushes(), 0)

        enqueue.assert_not_called()

    def test_the_sweep_drops_a_request_that_keeps_failing(self):
        from urbanlens.dashboard.tasks import MAX_CALENDAR_PUSH_ATTEMPTS, requeue_pending_calendar_pushes

        self._age_request(attempts=MAX_CALENDAR_PUSH_ATTEMPTS)

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_pending_calendar_pushes(), 0)

        enqueue.assert_not_called()
        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)

    def test_the_sweep_is_on_the_beat_schedule(self):
        from django.conf import settings

        from urbanlens.dashboard.tasks import requeue_pending_calendar_pushes

        names = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn(requeue_pending_calendar_pushes.name, names)
