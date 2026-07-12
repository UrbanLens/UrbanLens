"""Tests for the post_save signals that push auto-synced trips to Google Calendar."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
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
            name="Signal trip", creator=self.profile, start_date=datetime.date(2026, 11, 1), end_date=datetime.date(2026, 11, 2),
        )

    def _enqueue_for_trip_save(self):
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
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
            trip=self.trip, profile=self.profile, google_event_id="evt-1", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )

        enqueue = self._enqueue_for_trip_save()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.trip.pk)

    def test_trip_with_manual_export_link_does_not_enqueue(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-1", direction=CalendarSyncDirection.EXPORTED, auto_sync=False,
        )

        enqueue = self._enqueue_for_trip_save()
        enqueue.assert_not_called()

    def test_activity_save_enqueues_push_for_its_trip_when_auto_synced(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-1", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )
        activity = TripActivity.objects.create(trip=self.trip, title="New stop")

        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
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
