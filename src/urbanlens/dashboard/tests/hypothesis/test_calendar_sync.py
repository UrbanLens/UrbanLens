"""Tests for the per-user Google Calendar trip sync.

Covers:
- trip_to_event_body / event_to_trip_kwargs - pure conversion both ways,
  including the all-day exclusive-end-date convention (property-based)
- import_events_as_trips - trip/membership/link creation, dedupe, and
  skipping of events that originated as UrbanLens exports (gateway mocked)
- export_trip_to_calendar / remove_trip_from_calendar - event create vs
  update, vanished-event recreation, link bookkeeping (gateway mocked)
- OAuth callback view - rejects bad/missing state without storing tokens
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import TRIP_UUID_EVENT_PROPERTY, CalendarEventNotFoundError
from urbanlens.dashboard.services.calendar_sync import (
    event_originated_from_urbanlens,
    event_to_trip_kwargs,
    export_trip_to_calendar,
    import_events_as_trips,
    remove_trip_from_calendar,
    trip_to_event_body,
)

_DATES = st.dates(min_value=datetime.date(1990, 1, 1), max_value=datetime.date(2100, 1, 1))


class TripToEventBodyTests(TestCase):
    """trip_to_event_body maps trips to all-day event payloads."""

    @given(start=_DATES, duration_days=st.integers(min_value=0, max_value=90))
    def test_round_trip_preserves_name_and_dates(self, start, duration_days):
        """Exporting then re-importing an event yields the original trip dates."""
        end = start + datetime.timedelta(days=duration_days)
        trip = Trip(name="Round Trip", start_date=start, end_date=end)

        body = trip_to_event_body(trip)
        recovered = event_to_trip_kwargs(body)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["name"], "Round Trip")
        self.assertEqual(recovered["start_date"], start)
        self.assertEqual(recovered["end_date"], end)

    @given(start=_DATES, duration_days=st.integers(min_value=0, max_value=90))
    def test_all_day_end_is_exclusive(self, start, duration_days):
        """Google all-day end dates are exclusive: trip end + 1 day."""
        end = start + datetime.timedelta(days=duration_days)
        trip = Trip(name="X", start_date=start, end_date=end)

        body = trip_to_event_body(trip)

        self.assertEqual(body["start"]["date"], start.isoformat())
        self.assertEqual(body["end"]["date"], (end + datetime.timedelta(days=1)).isoformat())

    def test_marks_event_with_trip_uuid(self):
        trip = Trip(name="X", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 1))
        body = trip_to_event_body(trip)
        self.assertEqual(body["extendedProperties"]["private"][TRIP_UUID_EVENT_PROPERTY], str(trip.uuid))
        self.assertTrue(event_originated_from_urbanlens(body))

    def test_appends_trip_url_to_description(self):
        trip = Trip(name="X", description="Notes.", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 1))
        body = trip_to_event_body(trip, trip_url="https://example.com/trips/abc/")
        self.assertIn("Notes.", body["description"])
        self.assertIn("https://example.com/trips/abc/", body["description"])

    def test_raises_without_dates(self):
        trip = Trip.objects.create(name="Dateless")
        with self.assertRaises(ValueError):
            trip_to_event_body(trip)


class EventToTripKwargsTests(TestCase):
    """event_to_trip_kwargs parses Google event resources defensively."""

    def test_timed_event_uses_date_components(self):
        event = {
            "summary": "Explore mill",
            "start": {"dateTime": "2026-08-01T15:30:00Z"},
            "end": {"dateTime": "2026-08-01T18:00:00Z"},
        }
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["start_date"], datetime.date(2026, 8, 1))
        self.assertEqual(kwargs["end_date"], datetime.date(2026, 8, 1))

    def test_cancelled_event_rejected(self):
        event = {"status": "cancelled", "start": {"date": "2026-08-01"}, "end": {"date": "2026-08-02"}}
        self.assertIsNone(event_to_trip_kwargs(event))

    def test_missing_start_rejected(self):
        self.assertIsNone(event_to_trip_kwargs({"summary": "No dates"}))
        self.assertIsNone(event_to_trip_kwargs({"start": {"date": "not-a-date"}}))

    def test_end_before_start_clamped(self):
        event = {"start": {"date": "2026-08-10"}, "end": {"date": "2026-08-05"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["start_date"], datetime.date(2026, 8, 10))
        self.assertEqual(kwargs["end_date"], datetime.date(2026, 8, 10))

    def test_untitled_event_gets_fallback_name(self):
        event = {"start": {"date": "2026-08-01"}, "end": {"date": "2026-08-02"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["name"], "Imported calendar event")

    def test_long_summary_truncated(self):
        event = {"summary": "x" * 600, "start": {"date": "2026-08-01"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(len(kwargs["name"]), 255)


class _CalendarSyncDBTestCase(TestCase):
    """Shared fixtures: a profile with a connected calendar account."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="calendar-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )

    def _patch_gateway(self):
        """Patch the gateway class used by the sync service; returns the instance mock."""
        patcher = mock.patch("urbanlens.dashboard.services.calendar_sync.GoogleCalendarGateway")
        gateway_cls = patcher.start()
        self.addCleanup(patcher.stop)
        return gateway_cls.return_value


class ImportEventsTests(_CalendarSyncDBTestCase):
    """import_events_as_trips creates trips from the user's own events."""

    def test_import_creates_trip_membership_and_link(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt1",
            "summary": "Abandoned asylum weekend",
            "description": "Bring the wide lens.",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-07"},
        }

        created, skipped = import_events_as_trips(self.account, ["evt1"])

        self.assertEqual(len(created), 1)
        self.assertEqual(skipped, [])
        trip = created[0]
        self.assertEqual(trip.name, "Abandoned asylum weekend")
        self.assertEqual(trip.start_date, datetime.date(2026, 9, 4))
        self.assertEqual(trip.end_date, datetime.date(2026, 9, 6))  # exclusive end - 1
        self.assertEqual(trip.creator, self.profile)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.profile).exists())
        link = TripCalendarLink.objects.get(trip=trip, profile=self.profile)
        self.assertEqual(link.google_event_id, "evt1")
        self.assertEqual(link.direction, CalendarSyncDirection.IMPORTED)

    def test_import_skips_already_linked_event(self):
        gateway = self._patch_gateway()
        trip = Trip.objects.create(name="Existing", creator=self.profile)
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt1",
            direction=CalendarSyncDirection.IMPORTED,
        )

        created, skipped = import_events_as_trips(self.account, ["evt1"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)
        gateway.get_event.assert_not_called()

    def test_import_skips_events_exported_from_urbanlens(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt2",
            "summary": "Trip echo",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
            "extendedProperties": {"private": {TRIP_UUID_EVENT_PROPERTY: "some-uuid"}},
        }

        created, skipped = import_events_as_trips(self.account, ["evt2"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)
        self.assertFalse(Trip.objects.filter(name="Trip echo").exists())

    def test_import_skips_vanished_event(self):
        gateway = self._patch_gateway()
        gateway.get_event.side_effect = CalendarEventNotFoundError("gone")

        created, skipped = import_events_as_trips(self.account, ["evt3"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)


class ExportTripTests(_CalendarSyncDBTestCase):
    """export_trip_to_calendar mirrors trips onto the user's own calendar."""

    def _trip(self) -> Trip:
        return Trip.objects.create(
            name="Export me",
            creator=self.profile,
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2026, 10, 3),
        )

    def test_export_creates_event_and_link(self):
        gateway = self._patch_gateway()
        gateway.create_event.return_value = {"id": "new-evt"}
        trip = self._trip()

        link = export_trip_to_calendar(self.account, trip, trip_url="https://example.com/t/")

        gateway.create_event.assert_called_once()
        body = gateway.create_event.call_args[0][0]
        self.assertEqual(body["summary"], "Export me")
        self.assertEqual(body["start"]["date"], "2026-10-01")
        self.assertEqual(body["end"]["date"], "2026-10-04")
        self.assertEqual(link.google_event_id, "new-evt")
        self.assertEqual(link.direction, CalendarSyncDirection.EXPORTED)

    def test_export_updates_existing_event(self):
        gateway = self._patch_gateway()
        gateway.update_event.return_value = {"id": "evt-old"}
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-old",
            direction=CalendarSyncDirection.EXPORTED,
        )

        export_trip_to_calendar(self.account, trip)

        gateway.update_event.assert_called_once()
        gateway.create_event.assert_not_called()
        self.assertEqual(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).count(), 1)

    def test_export_recreates_vanished_event(self):
        gateway = self._patch_gateway()
        gateway.update_event.side_effect = CalendarEventNotFoundError("gone")
        gateway.create_event.return_value = {"id": "evt-new"}
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-gone",
            direction=CalendarSyncDirection.EXPORTED,
        )

        link = export_trip_to_calendar(self.account, trip)

        gateway.create_event.assert_called_once()
        self.assertEqual(link.google_event_id, "evt-new")

    def test_remove_deletes_event_and_link(self):
        gateway = self._patch_gateway()
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-x",
            direction=CalendarSyncDirection.EXPORTED,
        )

        removed = remove_trip_from_calendar(self.account, trip)

        self.assertTrue(removed)
        gateway.delete_event.assert_called_once_with("evt-x")
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_remove_without_link_is_noop(self):
        gateway = self._patch_gateway()
        trip = self._trip()

        removed = remove_trip_from_calendar(self.account, trip)

        self.assertFalse(removed)
        gateway.delete_event.assert_not_called()


class CalendarCallbackViewTests(TestCase):
    """The OAuth callback rejects tampered or missing state without saving tokens."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="callback-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)

    def test_bad_state_redirects_without_creating_account(self):
        response = self.client.get(reverse("trips.calendar.callback"), {"state": "forged", "code": "abc"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())

    def test_provider_error_redirects_without_creating_account(self):
        response = self.client.get(reverse("trips.calendar.callback"), {"error": "access_denied"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())
