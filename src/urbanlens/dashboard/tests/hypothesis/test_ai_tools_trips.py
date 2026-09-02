"""Tests for services.ai.tools.trips - list/create/add_trip_activity.

create_trip/add_trip_activity are writes: registry.execute() refuses to run
them at all under ProcessRole.AI (see test_ai_tools_registry.py's
WriteRefusalUnderAiRoleTests) - the tests below call execute() under the
default (non-AI) role, matching where they'll actually run once the confirm
flow (batch 2d) executes a stored proposal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, execute


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


class ListTripsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()

    def _upcoming(self, **kwargs) -> Trip:
        return baker.make(Trip, start_date=date.today() + timedelta(days=3), **kwargs)

    def test_only_sees_own_trips(self) -> None:
        mine = self._upcoming(name="My Trip", creator=self.profile)
        TripMembership.objects.create(trip=mine, profile=self.profile, status=TripMembership.STATUS_JOINED)
        theirs = self._upcoming(name="Not Mine", creator=self.other)
        TripMembership.objects.create(trip=theirs, profile=self.other, status=TripMembership.STATUS_JOINED)

        result = execute("list_trips", {}, _context(self.profile))
        slugs = [row["slug"] for row in result.data["trips"]]
        self.assertEqual(slugs, [mine.slug])

    def test_shared_trip_is_visible_to_a_member(self) -> None:
        # VISIBLE_SHARED scope: a trip this profile didn't create but was
        # invited to and joined is legitimately visible.
        shared = self._upcoming(name="Shared Trip", creator=self.other)
        TripMembership.objects.create(trip=shared, profile=self.other, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=shared, profile=self.profile, status=TripMembership.STATUS_JOINED)

        result = execute("list_trips", {}, _context(self.profile))
        slugs = [row["slug"] for row in result.data["trips"]]
        self.assertIn(shared.slug, slugs)

    def test_name_is_wrapped_as_user_content(self) -> None:
        trip = self._upcoming(name="My Trip", creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)

        result = execute("list_trips", {}, _context(self.profile))
        self.assertTrue(result.data["trips"][0]["name"].startswith("<USER_DATA>"))


class CreateTripTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_creates_trip_and_membership(self) -> None:
        result = execute("create_trip", {"name": "Assistant Run"}, _context(self.profile))
        self.assertNotIn("error", result.data)
        trip = Trip.objects.get(slug=result.data["created"]["slug"])
        self.assertEqual(trip.creator_id, self.profile.id)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED).exists())

    def test_blank_name_generates_one(self) -> None:
        result = execute("create_trip", {}, _context(self.profile))
        self.assertTrue(result.data["created"]["name"].strip())

    def test_respects_upcoming_trip_limit(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = 1
        settings.save()

        first = execute("create_trip", {"name": "First Trip"}, _context(self.profile))
        first_trip = Trip.objects.get(slug=first.data["created"]["slug"])
        first_trip.start_date = date.today() + timedelta(days=3)
        first_trip.save(update_fields=["start_date"])

        blocked = execute("create_trip", {"name": "Second Trip"}, _context(self.profile))
        self.assertIn("error", blocked.data)
        self.assertFalse(Trip.objects.filter(name="Second Trip").exists())

    def test_write_tool_is_summarized_not_a_read_summary(self) -> None:
        result = execute("create_trip", {"name": "Labeled Trip"}, _context(self.profile))
        self.assertEqual(result.summary, "Created a trip")


class AddTripActivityTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.location = baker.make(Location, latitude="42.5", longitude="-73.5")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Steel Mill", name_is_user_provided=True)
        self.foreign_pin = baker.make(Pin, profile=self.other, location=self.location, name="Not Yours", name_is_user_provided=True)
        trip_result = execute("create_trip", {"name": "Scoped Trip"}, _context(self.profile))
        self.trip_slug = trip_result.data["created"]["slug"]

    def test_foreign_trip_is_rejected(self) -> None:
        foreign_trip = baker.make(Trip, name="Not Yours Either", creator=self.other)
        result = execute("add_trip_activity", {"trip_slug": foreign_trip.slug, "pin_slug": self.pin.slug}, _context(self.profile))
        self.assertIn("error", result.data)
        self.assertEqual(TripActivity.objects.filter(trip=foreign_trip).count(), 0)

    def test_foreign_pin_is_rejected(self) -> None:
        result = execute("add_trip_activity", {"trip_slug": self.trip_slug, "pin_slug": self.foreign_pin.slug}, _context(self.profile))
        self.assertIn("error", result.data)
        self.assertEqual(TripActivity.objects.filter(pin=self.foreign_pin).count(), 0)

    def test_adds_activity_with_scheduled_date(self) -> None:
        result = execute("add_trip_activity", {"trip_slug": self.trip_slug, "pin_slug": self.pin.slug, "scheduled_date": "2026-08-01"}, _context(self.profile))
        self.assertNotIn("error", result.data)
        activity = TripActivity.objects.get(pk=result.data["added"]["activity_id"])
        self.assertEqual(activity.status, TripActivity.STATUS_PROPOSED)
        self.assertEqual(activity.pin_id, self.pin.id)
        self.assertIsNotNone(activity.scheduled_at)

    def test_respects_activity_limit(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_activities = 1
        settings.save()
        # A profile can only have one pin per location (db_pin_unique_location_per_profile) - needs its own.
        second_location = baker.make(Location, latitude="43.0", longitude="-74.0")
        second_pin = baker.make(Pin, profile=self.profile, location=second_location, name="Second Pin", name_is_user_provided=True)

        first = execute("add_trip_activity", {"trip_slug": self.trip_slug, "pin_slug": self.pin.slug}, _context(self.profile))
        self.assertNotIn("error", first.data)

        blocked = execute("add_trip_activity", {"trip_slug": self.trip_slug, "pin_slug": second_pin.slug}, _context(self.profile))
        self.assertIn("error", blocked.data)
        self.assertEqual(TripActivity.objects.filter(trip__slug=self.trip_slug).count(), 1)

    def test_trip_and_pin_names_are_wrapped_as_user_content(self) -> None:
        result = execute("add_trip_activity", {"trip_slug": self.trip_slug, "pin_slug": self.pin.slug}, _context(self.profile))
        self.assertTrue(result.data["added"]["trip"].startswith("<USER_DATA>"))
        self.assertTrue(result.data["added"]["pin"].startswith("<USER_DATA>"))
