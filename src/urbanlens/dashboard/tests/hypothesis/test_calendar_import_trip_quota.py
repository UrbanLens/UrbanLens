"""Calendar import counts against ``max_upcoming_trips_per_user`` like every other way of making a trip."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import EventListing
from urbanlens.dashboard.services.trips.calendar_sync import import_events_as_trips, import_summary


def _event(event_id: str, summary: str, days_ahead: int) -> dict:
    start = timezone.now().date() + datetime.timedelta(days=days_ahead)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date": start.isoformat()},
        "end": {"date": (start + datetime.timedelta(days=1)).isoformat()},
    }


class CalendarImportTripQuotaTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="calendar-quota")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        patcher = mock.patch("urbanlens.dashboard.services.trips.calendar_sync.GoogleCalendarGateway")
        self.gateway = patcher.start().return_value
        self.addCleanup(patcher.stop)

    def _cap(self, limit: int) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = limit
        settings.save()

    def _make_upcoming_trip(self) -> None:
        trip = Trip.objects.create(
            name="Existing", creator=self.profile, start_date=timezone.now().date() + datetime.timedelta(days=3)
        )
        TripMembership.objects.get_or_create(trip=trip, profile=self.profile, defaults={"rsvp": "yes"})

    def test_trips_past_the_cap_are_refused_and_the_rest_still_import(self) -> None:
        self._cap(2)
        events = [_event("e1", "First", 10), _event("e2", "Second", 11), _event("e3", "Third", 12)]
        self.gateway.list_events.return_value = EventListing(events)

        created, skipped, _invited = import_events_as_trips(self.account, ["e1", "e2", "e3"])

        self.assertEqual([trip.name for trip in created], ["First", "Second"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("Third", skipped[0])
        self.assertIn("maximum of 2", skipped[0])
        self.assertFalse(Trip.objects.filter(name="Third").exists())
        self.assertFalse(
            TripCalendarLink.objects.filter(google_event_id="e3").exists(), "a refused event must stay importable later"
        )

    def test_trips_already_on_the_account_count_toward_the_cap(self) -> None:
        self._cap(1)
        self._make_upcoming_trip()
        self.gateway.list_events.return_value = EventListing([_event("e1", "Only", 10)])

        created, skipped, _invited = import_events_as_trips(self.account, ["e1"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)
        level, message = import_summary(len(created), skipped, 0)
        self.assertEqual(level, "warning")
        self.assertIn("maximum of 1", message)

    def test_zero_means_unlimited(self) -> None:
        self._cap(0)
        self._make_upcoming_trip()
        self._make_upcoming_trip()
        self.gateway.list_events.return_value = EventListing([_event("e1", "One", 10), _event("e2", "Two", 11)])

        created, skipped, _invited = import_events_as_trips(self.account, ["e1", "e2"])

        self.assertEqual(len(created), 2)
        self.assertEqual(skipped, [])
