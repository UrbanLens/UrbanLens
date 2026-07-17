"""Tests for TripCalendarLinkQuerySet.

Part of the ongoing "every model gets its own queryset/manager" cleanup -
TripCalendarLink (and GoogleCalendarAccount, covered by
test_calendar_sync.py::GetCalendarAccountTests) were still on the bare
default manager despite several genuinely duplicated call-site shapes across
controllers/services/calendar_sync.py.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, TripCalendarLink
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity


class TripCalendarLinkTripLevelLinkTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="link-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.trip = Trip.objects.create(name="Ridgeline", creator=self.profile)

    def test_returns_the_trip_level_link_not_an_activity_link(self) -> None:
        activity = TripActivity.objects.create(trip=self.trip, title="Overlook")
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            activity=activity,
            direction=CalendarSyncDirection.EXPORTED,
            google_event_id="activity-event",
        )
        trip_level = TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            direction=CalendarSyncDirection.EXPORTED,
            google_event_id="trip-event",
        )
        self.assertEqual(TripCalendarLink.objects.trip_level_link(self.trip, self.profile), trip_level)

    def test_returns_none_when_no_trip_level_link_exists(self) -> None:
        self.assertIsNone(TripCalendarLink.objects.trip_level_link(self.trip, self.profile))


class TripCalendarLinkActivityLinksByIdTests(TestCase):
    def test_maps_each_link_to_its_activity_id_and_excludes_the_trip_level_link(self) -> None:
        user = User.objects.create_user(username="activity-link-tester")
        profile, _ = Profile.objects.get_or_create(user=user)
        trip = Trip.objects.create(name="Backcountry", creator=profile)
        activity_a = TripActivity.objects.create(trip=trip, title="Trailhead")
        activity_b = TripActivity.objects.create(trip=trip, title="Summit")
        link_a = TripCalendarLink.objects.create(trip=trip, profile=profile, activity=activity_a, direction=CalendarSyncDirection.EXPORTED, google_event_id="a")
        link_b = TripCalendarLink.objects.create(trip=trip, profile=profile, activity=activity_b, direction=CalendarSyncDirection.EXPORTED, google_event_id="b")
        TripCalendarLink.objects.create(trip=trip, profile=profile, direction=CalendarSyncDirection.EXPORTED, google_event_id="trip")

        result = TripCalendarLink.objects.activity_links_by_activity_id(trip, profile)
        self.assertEqual(result, {activity_a.pk: link_a, activity_b.pk: link_b})


class TripCalendarLinkAlreadyLinkedTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="already-linked-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.trip = Trip.objects.create(name="Coastal", creator=self.profile)

    def test_true_when_a_link_exists_for_this_profile_and_event(self) -> None:
        TripCalendarLink.objects.create(trip=self.trip, profile=self.profile, direction=CalendarSyncDirection.IMPORTED, google_event_id="ev-1")
        self.assertTrue(TripCalendarLink.objects.already_linked(self.profile, "ev-1"))

    def test_false_for_an_unlinked_event(self) -> None:
        self.assertFalse(TripCalendarLink.objects.already_linked(self.profile, "ev-unknown"))

    def test_false_for_a_different_profiles_link_to_the_same_event(self) -> None:
        other_user = User.objects.create_user(username="other-profile-tester")
        other_profile, _ = Profile.objects.get_or_create(user=other_user)
        TripCalendarLink.objects.create(trip=self.trip, profile=other_profile, direction=CalendarSyncDirection.IMPORTED, google_event_id="ev-1")
        self.assertFalse(TripCalendarLink.objects.already_linked(self.profile, "ev-1"))


class TripCalendarLinkSetAutoSyncTests(TestCase):
    def test_updates_only_the_auto_sync_flag(self) -> None:
        user = User.objects.create_user(username="auto-sync-tester")
        profile, _ = Profile.objects.get_or_create(user=user)
        trip = Trip.objects.create(name="Roadtrip", creator=profile)
        link = TripCalendarLink.objects.create(trip=trip, profile=profile, direction=CalendarSyncDirection.EXPORTED, google_event_id="ev-1", auto_sync=False)

        TripCalendarLink.objects.set_auto_sync(link.pk, auto_sync=True)

        link.refresh_from_db()
        self.assertTrue(link.auto_sync)
