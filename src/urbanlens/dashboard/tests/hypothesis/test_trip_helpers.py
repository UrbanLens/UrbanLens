"""Tests for trip controller helper functions (pure logic, no DB needed where possible).

Covers:
- _parse_scheduled_at() - date/time string parsing
- _activity_coords() - coordinate resolution with override/pin/location priority
- _expand_trip_dates() - trip date range expansion
- _is_organizer() - organizer detection
- _can_perform() - permission level checking
- _compute_activity_index_map() - map-index assignment
- _build_activity_forecasts() - weather slot matching
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.trip import (
    _activity_coords,
    _build_activity_forecasts,
    _can_perform,
    _compute_activity_index_map,
    _expand_trip_dates,
    _is_organizer,
    _parse_scheduled_at,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_hyp = hyp_settings(max_examples=40, deadline=None)


# ---------------------------------------------------------------------------
# _parse_scheduled_at
# ---------------------------------------------------------------------------

class ParseScheduledAtTests(SimpleTestCase):
    """_parse_scheduled_at combines ISO date and time strings."""

    def test_date_only_returns_midnight(self):
        result = _parse_scheduled_at("2025-06-15", None)
        self.assertIsNotNone(result)
        self.assertEqual(result.date(), datetime.date(2025, 6, 15))
        self.assertEqual(result.time(), datetime.time(0, 0))

    def test_date_and_time_combined(self):
        result = _parse_scheduled_at("2025-06-15", "14:30")
        self.assertEqual(result.date(), datetime.date(2025, 6, 15))
        self.assertEqual(result.time(), datetime.time(14, 30))

    def test_invalid_time_falls_back_to_midnight(self):
        result = _parse_scheduled_at("2025-06-15", "not-a-time")
        self.assertEqual(result.time(), datetime.time(0, 0))

    def test_no_date_returns_none(self):
        self.assertIsNone(_parse_scheduled_at(None, "14:30"))

    def test_empty_date_string_returns_none(self):
        self.assertIsNone(_parse_scheduled_at("", "14:30"))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(_parse_scheduled_at("not-a-date", "14:30"))

    def test_returns_datetime_instance(self):
        result = _parse_scheduled_at("2025-01-01", "00:00")
        self.assertIsInstance(result, datetime.datetime)

    @given(
        year=st.integers(min_value=2000, max_value=2099),
        month=st.integers(min_value=1, max_value=12),
        day=st.integers(min_value=1, max_value=28),
    )
    @_hyp
    def test_valid_dates_always_parse(self, year: int, month: int, day: int):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        result = _parse_scheduled_at(date_str, None)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# _activity_coords
# ---------------------------------------------------------------------------

class ActivityCoordsTests(SimpleTestCase):
    """_activity_coords resolves coordinates with correct priority."""

    def _make_activity(self, lat_override=None, lng_override=None, pin=None, location=None):
        act = MagicMock()
        act.lat_override = lat_override
        act.lng_override = lng_override
        act.pin = pin
        act.location = location
        return act

    def _make_pin(self, lat=None, lng=None):
        pin = MagicMock()
        pin.effective_latitude = lat
        pin.effective_longitude = lng
        return pin

    def _make_location(self, lat=None, lng=None):
        loc = MagicMock()
        loc.latitude = lat
        loc.longitude = lng
        return loc

    def test_override_takes_priority_over_pin(self):
        pin = self._make_pin(lat=10.0, lng=20.0)
        act = self._make_activity(lat_override=1.0, lng_override=2.0, pin=pin)
        result = _activity_coords(act)
        self.assertEqual(result, (1.0, 2.0))

    def test_pin_coords_used_when_no_override(self):
        pin = self._make_pin(lat=51.5, lng=-0.12)
        act = self._make_activity(pin=pin)
        result = _activity_coords(act)
        self.assertEqual(result, (51.5, -0.12))

    def test_location_coords_used_when_no_pin(self):
        loc = self._make_location(lat=48.85, lng=2.35)
        act = self._make_activity(location=loc)
        result = _activity_coords(act)
        self.assertEqual(result, (48.85, 2.35))

    def test_none_returned_when_no_coords(self):
        act = self._make_activity()
        self.assertIsNone(_activity_coords(act))

    def test_none_returned_when_pin_has_no_coords(self):
        pin = self._make_pin(lat=None, lng=None)
        act = self._make_activity(pin=pin)
        self.assertIsNone(_activity_coords(act))

    def test_partial_override_falls_through(self):
        # lat_override present but lng_override missing - should not use override
        act = self._make_activity(lat_override=1.0, lng_override=None)
        act.pin = None
        act.location = self._make_location(lat=48.0, lng=2.0)
        result = _activity_coords(act)
        # Override requires BOTH lat and lng
        self.assertEqual(result, (48.0, 2.0))

    def test_location_coords_are_converted_to_float(self):
        loc = self._make_location(lat=51, lng=-0)
        act = self._make_activity(location=loc)
        result = _activity_coords(act)
        self.assertIsInstance(result[0], float)


# ---------------------------------------------------------------------------
# _expand_trip_dates (DB-backed)
# ---------------------------------------------------------------------------

class ExpandTripDatesTests(TestCase):
    """_expand_trip_dates extends the trip date range as needed."""

    def _make_trip(self, start=None, end=None):
        user = baker.make("auth.User")
        profile = user.profile
        return Trip.objects.create(name="Test Trip", creator=profile, start_date=start, end_date=end)

    def test_sets_start_date_when_none(self):
        trip = self._make_trip(start=None, end=None)
        _expand_trip_dates(trip, datetime.date(2025, 7, 4))
        trip.refresh_from_db()
        self.assertEqual(trip.start_date, datetime.date(2025, 7, 4))

    def test_sets_end_date_when_none(self):
        trip = self._make_trip(start=None, end=None)
        _expand_trip_dates(trip, datetime.date(2025, 7, 4))
        trip.refresh_from_db()
        self.assertEqual(trip.end_date, datetime.date(2025, 7, 4))

    def test_expands_start_when_activity_earlier(self):
        trip = self._make_trip(start=datetime.date(2025, 8, 1), end=datetime.date(2025, 8, 10))
        _expand_trip_dates(trip, datetime.date(2025, 7, 25))
        trip.refresh_from_db()
        self.assertEqual(trip.start_date, datetime.date(2025, 7, 25))

    def test_expands_end_when_activity_later(self):
        trip = self._make_trip(start=datetime.date(2025, 8, 1), end=datetime.date(2025, 8, 10))
        _expand_trip_dates(trip, datetime.date(2025, 8, 20))
        trip.refresh_from_db()
        self.assertEqual(trip.end_date, datetime.date(2025, 8, 20))

    def test_no_change_when_date_within_range(self):
        trip = self._make_trip(start=datetime.date(2025, 8, 1), end=datetime.date(2025, 8, 10))
        _expand_trip_dates(trip, datetime.date(2025, 8, 5))
        trip.refresh_from_db()
        self.assertEqual(trip.start_date, datetime.date(2025, 8, 1))
        self.assertEqual(trip.end_date, datetime.date(2025, 8, 10))


# ---------------------------------------------------------------------------
# _is_organizer (DB-backed)
# ---------------------------------------------------------------------------

class IsOrganizerTests(TestCase):
    """_is_organizer returns True for creators and designated organizers."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = Trip.objects.create(name="Org Trip", creator=self.creator)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def test_creator_is_organizer(self):
        self.assertTrue(_is_organizer(self.creator, self.trip))

    def test_plain_member_not_organizer(self):
        self.assertFalse(_is_organizer(self.member, self.trip))

    def test_promoted_member_is_organizer(self):
        TripMembership.objects.filter(trip=self.trip, profile=self.member).update(is_organizer=True)
        self.assertTrue(_is_organizer(self.member, self.trip))

    def test_non_member_not_organizer(self):
        outsider = baker.make("auth.User").profile
        self.assertFalse(_is_organizer(outsider, self.trip))


# ---------------------------------------------------------------------------
# _can_perform (DB-backed)
# ---------------------------------------------------------------------------

class CanPerformTests(TestCase):
    """_can_perform checks permission level against profile's relationship to trip."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = Trip.objects.create(
            name="Perm Trip",
            creator=self.creator,
            allow_add_activities=Trip.PERM_EVERYONE,
        )
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def test_creator_can_always_perform(self):
        for level in (Trip.PERM_NONE, Trip.PERM_ORGANIZERS, Trip.PERM_EVERYONE):
            with self.subTest(level=level):
                self.assertTrue(_can_perform(self.creator, self.trip, level))

    def test_member_can_perform_when_everyone(self):
        self.assertTrue(_can_perform(self.member, self.trip, Trip.PERM_EVERYONE))

    def test_member_cannot_perform_when_organizers(self):
        self.assertFalse(_can_perform(self.member, self.trip, Trip.PERM_ORGANIZERS))

    def test_member_cannot_perform_when_none(self):
        self.assertFalse(_can_perform(self.member, self.trip, Trip.PERM_NONE))

    def test_organizer_can_perform_when_organizers(self):
        TripMembership.objects.filter(trip=self.trip, profile=self.member).update(is_organizer=True)
        self.assertTrue(_can_perform(self.member, self.trip, Trip.PERM_ORGANIZERS))


# ---------------------------------------------------------------------------
# _compute_activity_index_map
# ---------------------------------------------------------------------------

class ComputeActivityIndexMapTests(SimpleTestCase):
    """_compute_activity_index_map assigns sequential 1-based indices to visible activities."""

    def _make_activity(self, coords=True, hidden=False, status=TripActivity.STATUS_PROPOSED):
        act = MagicMock()
        act.id = id(act)
        act.location_hidden = hidden
        act.status = status
        if coords:
            act.lat_override = 10.0
            act.lng_override = 20.0
            act.pin = None
            act.location = None
        else:
            act.lat_override = None
            act.lng_override = None
            act.pin = None
            act.location = None
        return act

    def test_empty_activities_returns_empty_map(self):
        self.assertEqual(_compute_activity_index_map([]), {})

    def test_visible_activities_get_sequential_indices(self):
        acts = [self._make_activity() for _ in range(3)]
        result = _compute_activity_index_map(acts)
        self.assertEqual(set(result.values()), {1, 2, 3})

    def test_hidden_activities_excluded(self):
        acts = [
            self._make_activity(hidden=False),
            self._make_activity(hidden=True),
            self._make_activity(hidden=False),
        ]
        result = _compute_activity_index_map(acts)
        self.assertEqual(len(result), 2)

    def test_completed_activities_excluded(self):
        acts = [
            self._make_activity(status=TripActivity.STATUS_PROPOSED),
            self._make_activity(status=TripActivity.STATUS_COMPLETED),
        ]
        result = _compute_activity_index_map(acts)
        self.assertEqual(len(result), 1)

    def test_no_coords_activities_excluded(self):
        acts = [
            self._make_activity(coords=True),
            self._make_activity(coords=False),
        ]
        result = _compute_activity_index_map(acts)
        self.assertEqual(len(result), 1)

    def test_indices_start_at_one(self):
        acts = [self._make_activity() for _ in range(2)]
        result = _compute_activity_index_map(acts)
        self.assertIn(1, result.values())

    @given(n=st.integers(min_value=0, max_value=20))
    @_hyp
    def test_n_visible_activities_get_n_indices(self, n: int):
        acts = [self._make_activity() for _ in range(n)]
        result = _compute_activity_index_map(acts)
        self.assertEqual(len(result), n)


# ---------------------------------------------------------------------------
# _build_activity_forecasts
# ---------------------------------------------------------------------------

class BuildActivityForecastsTests(SimpleTestCase):
    """_build_activity_forecasts matches activities to weather slots."""

    def _make_activity(self, lat=51.5, lng=-0.12, scheduled_at=None, status="proposed"):
        act = MagicMock()
        act.lat_override = lat
        act.lng_override = lng
        act.pin = None
        act.location = MagicMock()
        act.location.display_name = "Test Location"
        act.title = None
        act.scheduled_at = scheduled_at
        act.status = status
        return act

    def _make_gateway(self, slots=None):
        gw = MagicMock()
        gw.get_raw_forecast.return_value = slots or []
        return gw

    def test_activity_without_scheduled_at_gets_no_slot(self):
        act = self._make_activity(scheduled_at=None)
        gw = self._make_gateway()
        results = _build_activity_forecasts([act], gw)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["slot"])

    def test_activity_without_coords_marked_no_coords(self):
        act = self._make_activity(lat=None, lng=None, scheduled_at=datetime.datetime(2025, 7, 4, 12, 0))
        act.lat_override = None
        act.lng_override = None
        act.pin = None
        act.location = None
        gw = self._make_gateway()
        results = _build_activity_forecasts([act], gw)
        self.assertTrue(results[0]["no_coords"])

    def test_slot_matched_when_within_36_hours(self):
        target = datetime.datetime(2025, 7, 4, 12, 0)
        slot_time = datetime.datetime(2025, 7, 4, 12, 0)
        slot = {"date": slot_time, "temp": 22, "description": "Sunny"}
        act = self._make_activity(scheduled_at=target)
        gw = self._make_gateway(slots=[slot])
        results = _build_activity_forecasts([act], gw)
        self.assertIsNotNone(results[0]["slot"])
        self.assertEqual(results[0]["slot"]["temp"], 22)

    def test_out_of_range_when_gap_exceeds_36h(self):
        target = datetime.datetime(2025, 7, 4, 12, 0)
        slot_time = datetime.datetime(2025, 7, 6, 18, 0)  # ~54h gap
        slot = {"date": slot_time, "temp": 15, "description": "Cloudy"}
        act = self._make_activity(scheduled_at=target)
        gw = self._make_gateway(slots=[slot])
        results = _build_activity_forecasts([act], gw)
        self.assertTrue(results[0]["out_of_range"])

    def test_gateway_exception_returns_no_slot(self):
        import requests as req_lib
        target = datetime.datetime(2025, 7, 4, 12, 0)
        act = self._make_activity(scheduled_at=target)
        gw = MagicMock()
        gw.get_raw_forecast.side_effect = req_lib.RequestException("timeout")
        results = _build_activity_forecasts([act], gw)
        self.assertIsNone(results[0]["slot"])

    def test_coords_cached_across_same_location(self):
        target = datetime.datetime(2025, 7, 4, 12, 0)
        slot = {"date": target, "temp": 20, "description": "Clear"}
        gw = self._make_gateway(slots=[slot])
        # Two activities at the same rounded coords
        acts = [
            self._make_activity(lat=51.5, lng=-0.12, scheduled_at=target),
            self._make_activity(lat=51.5, lng=-0.12, scheduled_at=target),
        ]
        _build_activity_forecasts(acts, gw)
        # Gateway should only be called once for the same coord pair
        self.assertEqual(gw.get_raw_forecast.call_count, 1)


# ---------------------------------------------------------------------------
# TripWeatherView - drops activities with nothing useful to show instead of
# rendering an empty "No location data"/"Outside 5-day forecast" row
# ---------------------------------------------------------------------------

class TripWeatherViewFiltersEmptyForecastsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        self.client_ = Client()
        self.client_.force_login(self.user)
        self.trip = Trip.objects.create(name="Test Trip", creator=self.profile)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.profile, defaults={"rsvp": "yes"})

    def _url(self) -> str:
        return reverse("trips.weather", args=[self.trip.slug])

    def _tomorrow(self) -> datetime.datetime:
        return timezone.now() + datetime.timedelta(days=1)

    def test_activity_with_no_location_is_dropped_and_panel_hides(self) -> None:
        baker.make(
            TripActivity,
            trip=self.trip,
            title="Campground",
            scheduled_at=self._tomorrow(),
            lat_override=None,
            lng_override=None,
            pin=None,
            location=None,
        )

        with patch("urbanlens.UrbanLens.settings.app.settings.openweathermap_api_key", "fake-key"):
            resp = self.client_.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["grouped"], [])
        content = resp.content.decode()
        self.assertNotIn("No location data", content)
        self.assertNotIn("Outside 5-day forecast", content)

    def test_activity_with_a_matched_forecast_slot_is_kept(self) -> None:
        target = self._tomorrow()
        baker.make(
            TripActivity,
            trip=self.trip,
            title="Museum",
            scheduled_at=target,
            lat_override=51.5,
            lng_override=-0.12,
            pin=None,
            location=None,
        )
        slot = {
            "date": target.replace(tzinfo=None),
            "main": {"temp": 20, "feels_like": 18, "humidity": 50},
            "weather": [{"icon": "01d", "description": "Clear"}],
            "wind": {"speed": 5},
        }
        gw = MagicMock()
        gw.get_raw_forecast.return_value = [slot]

        with (
            patch("urbanlens.UrbanLens.settings.app.settings.openweathermap_api_key", "fake-key"),
            patch("urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway", return_value=gw),
        ):
            resp = self.client_.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["grouped"])
        self.assertIn("Clear", resp.content.decode())

    def test_mixed_activities_only_the_matched_one_survives(self) -> None:
        target = self._tomorrow()
        baker.make(
            TripActivity,
            trip=self.trip,
            title="Campground",
            scheduled_at=target,
            lat_override=None,
            lng_override=None,
            pin=None,
            location=None,
        )
        baker.make(
            TripActivity,
            trip=self.trip,
            title="Museum",
            scheduled_at=target,
            lat_override=51.5,
            lng_override=-0.12,
            pin=None,
            location=None,
        )
        slot = {
            "date": target.replace(tzinfo=None),
            "main": {"temp": 20, "feels_like": 18, "humidity": 50},
            "weather": [{"icon": "01d", "description": "Clear"}],
            "wind": {"speed": 5},
        }
        gw = MagicMock()
        gw.get_raw_forecast.return_value = [slot]

        with (
            patch("urbanlens.UrbanLens.settings.app.settings.openweathermap_api_key", "fake-key"),
            patch("urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway", return_value=gw),
        ):
            resp = self.client_.get(self._url())

        grouped = resp.context["grouped"]
        self.assertEqual(len(grouped), 1)
        _day, entries = grouped[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["location_name"], "Museum")


# ---------------------------------------------------------------------------
# activities_with_index's has_coords - drives the Activities panel's
# "Needs location" badge
# ---------------------------------------------------------------------------

class ActivitiesPanelHasCoordsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client_ = Client()
        self.client_.force_login(self.user)
        self.trip = Trip.objects.create(name="Test Trip", creator=self.profile)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.profile, defaults={"rsvp": "yes"})

    def test_activity_with_no_coords_shows_needs_location_badge(self) -> None:
        baker.make(TripActivity, trip=self.trip, title="Campground", lat_override=None, lng_override=None, pin=None, location=None)

        resp = self.client_.get(reverse("trips.activities", args=[self.trip.slug]))

        self.assertIn("Needs location", resp.content.decode())

    def test_activity_with_coords_does_not_show_needs_location_badge(self) -> None:
        baker.make(TripActivity, trip=self.trip, title="Museum", lat_override=51.5, lng_override=-0.12, pin=None, location=None)

        resp = self.client_.get(reverse("trips.activities", args=[self.trip.slug]))

        self.assertNotIn("Needs location", resp.content.decode())


# ---------------------------------------------------------------------------
# TripMembershipQuerySet - custom queryset/manager (previously the bare
# default manager, inconsistent with the rest of the codebase's convention)
# ---------------------------------------------------------------------------

class TripMembershipQuerySetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.creator_user: User = baker.make("auth.User")
        self.creator = Profile.objects.get(user=self.creator_user)
        self.trip = Trip.objects.create(name="Test Trip", creator=self.creator)
        self.member_user: User = baker.make("auth.User")
        self.member = Profile.objects.get(user=self.member_user)

    def test_for_trip_and_profile_matches_the_unique_pair(self) -> None:
        membership = TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp="yes")

        found = TripMembership.objects.for_trip_and_profile(self.trip, self.member).first()

        self.assertEqual(found, membership)

    def test_for_trip_and_profile_excludes_other_profiles(self) -> None:
        TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp="yes")
        other_user: User = baker.make("auth.User")
        other = Profile.objects.get(user=other_user)

        found = TripMembership.objects.for_trip_and_profile(self.trip, other).first()

        self.assertIsNone(found)

    def test_trip_ids_for_returns_every_trip_the_profile_belongs_to(self) -> None:
        other_trip = Trip.objects.create(name="Other Trip", creator=self.creator)
        TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp="yes")
        TripMembership.objects.create(trip=other_trip, profile=self.member, rsvp="yes")

        ids = set(TripMembership.objects.trip_ids_for(self.member))

        self.assertEqual(ids, {self.trip.pk, other_trip.pk})

    def test_trip_ids_for_excludes_other_profiles_trips(self) -> None:
        TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp="yes")
        other_user: User = baker.make("auth.User")
        other = Profile.objects.get(user=other_user)

        ids = set(TripMembership.objects.trip_ids_for(other))

        self.assertEqual(ids, set())

    def test_joined_includes_only_joined_status_members(self) -> None:
        joined = TripMembership.objects.create(trip=self.trip, profile=self.member, status=TripMembership.STATUS_JOINED)
        invited_user: User = baker.make("auth.User")
        invited_profile = Profile.objects.get(user=invited_user)
        TripMembership.objects.create(trip=self.trip, profile=invited_profile, status=TripMembership.STATUS_INVITED)

        result = list(TripMembership.objects.joined(self.trip))

        self.assertEqual(result, [joined])

    def test_rsvp_yes_includes_only_yes_responses(self) -> None:
        yes_member = TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp=TripMembership.RSVP_YES)
        maybe_user: User = baker.make("auth.User")
        maybe_profile = Profile.objects.get(user=maybe_user)
        TripMembership.objects.create(trip=self.trip, profile=maybe_profile, rsvp=TripMembership.RSVP_MAYBE)

        result = list(TripMembership.objects.rsvp_yes(self.trip))

        self.assertEqual(result, [yes_member])
