"""Tests for trip-planning upgrades: OSRM drive-time legs, optional trip names (UL-60/UL-360)."""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trip_legs import TripLeg, _cache_key, compute_legs
from urbanlens.dashboard.services.trip_names import random_trip_name, trip_name_suggestions


class TripLegDisplayTests(TestCase):
    """Pure formatting for leg chips."""

    def test_duration_display(self) -> None:
        self.assertEqual(TripLeg(distance_meters=1000, duration_seconds=20).duration_display, "1 min")
        self.assertEqual(TripLeg(distance_meters=1000, duration_seconds=38 * 60).duration_display, "38 min")
        self.assertEqual(TripLeg(distance_meters=1000, duration_seconds=125 * 60).duration_display, "2 hr 5 min")
        self.assertEqual(TripLeg(distance_meters=1000, duration_seconds=180 * 60).duration_display, "3 hr")

    def test_distance_display_in_miles(self) -> None:
        self.assertEqual(TripLeg(distance_meters=1609.34, duration_seconds=60).distance_display, "1.0 mi")
        self.assertEqual(TripLeg(distance_meters=29450, duration_seconds=60).distance_display, "18.3 mi")
        self.assertEqual(TripLeg(distance_meters=228_526, duration_seconds=60).distance_display, "142 mi")


class ComputeLegsTests(TestCase):
    """Cache behavior and the live-call budget."""

    def setUp(self) -> None:
        cache.clear()
        self.stops = [
            (1, (42.10000, -73.10000)),
            (2, (42.20000, -73.20000)),
            (3, (42.30000, -73.30000)),
        ]

    def test_cached_legs_never_touch_the_gateway(self) -> None:
        cache.set(_cache_key(self.stops[0][1], self.stops[1][1]), {"distance_meters": 18000.0, "duration_seconds": 1200.0})
        cache.set(_cache_key(self.stops[1][1], self.stops[2][1]), {"distance_meters": 9000.0, "duration_seconds": 600.0})
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            legs = compute_legs(self.stops)
        gateway_cls.assert_not_called()
        self.assertEqual(set(legs), {2, 3})
        self.assertEqual(legs[2].duration_display, "20 min")

    def test_live_calls_respect_the_budget_and_fill_the_cache(self) -> None:
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            gateway_cls.return_value.get_route_between.return_value = {"distance_meters": 5000.0, "duration_seconds": 300.0}
            legs = compute_legs(self.stops, max_live_calls=1)
        self.assertEqual(gateway_cls.return_value.get_route_between.call_count, 1)
        self.assertEqual(set(legs), {2})

        # Second render: leg 1→2 is cached now, so the budget covers 2→3.
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            gateway_cls.return_value.get_route_between.return_value = {"distance_meters": 7000.0, "duration_seconds": 420.0}
            legs = compute_legs(self.stops, max_live_calls=1)
        self.assertEqual(gateway_cls.return_value.get_route_between.call_count, 1)
        self.assertEqual(set(legs), {2, 3})

    def test_unroutable_pairs_are_negative_cached(self) -> None:
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            gateway_cls.return_value.get_route_between.return_value = None
            legs = compute_legs(self.stops[:2])
        self.assertEqual(legs, {})
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            legs = compute_legs(self.stops[:2])
        gateway_cls.assert_not_called()
        self.assertEqual(legs, {})

    def test_identical_consecutive_stops_are_skipped(self) -> None:
        with patch("urbanlens.dashboard.services.trip_legs.OSRMGateway") as gateway_cls:
            legs = compute_legs([(1, (42.1, -73.1)), (2, (42.1, -73.1))])
        gateway_cls.assert_not_called()
        self.assertEqual(legs, {})


class TripNameTests(TestCase):
    """Generated trip names (UL-360)."""

    def test_random_trip_name_is_nonempty_two_parter(self) -> None:
        for _ in range(20):
            name = random_trip_name()
            self.assertTrue(name.strip())
            self.assertGreaterEqual(len(name.split()), 2)

    def test_suggestions_are_distinct(self) -> None:
        suggestions = trip_name_suggestions(8)
        self.assertEqual(len(suggestions), len(set(suggestions)))

    def test_create_trip_with_blank_name_gets_a_generated_one(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        user = baker.make("auth.User")
        self.client.force_login(user)
        response = self.client.post(reverse("trips.create"), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        trip = Trip.objects.get()
        self.assertTrue(trip.name.strip())


class ActivitiesPanelLegTests(TestCase):
    """Legs render between visible stops and never leak hidden locations."""

    def setUp(self) -> None:
        cache.clear()
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.trip = baker.make(Trip, name="Leg Test Trip", creator=self.profile)
        baker.make(TripMembership, trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED, rsvp="yes")
        self.loc_a = baker.make(Location, latitude="42.100000", longitude="-73.100000")
        self.loc_b = baker.make(Location, latitude="42.200000", longitude="-73.200000")
        self.act_a = baker.make(TripActivity, trip=self.trip, location=self.loc_a, added_by=self.profile, title="First stop", order=0)
        self.act_b = baker.make(TripActivity, trip=self.trip, location=self.loc_b, added_by=self.profile, title="Second stop", order=1)
        self.client.force_login(self.user)
        self.url = reverse("trips.activities", args=[self.trip.slug])

    def _seed_leg_cache(self) -> None:
        cache.set(
            _cache_key((42.1, -73.1), (42.2, -73.2)),
            {"distance_meters": 29450.0, "duration_seconds": 2280.0},
        )

    def test_leg_chip_renders_between_stops(self) -> None:
        self._seed_leg_cache()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "trip-activity-leg")
        self.assertContains(response, "38 min")
        self.assertContains(response, "18.3 mi")

    def test_hidden_location_stops_produce_no_leg(self) -> None:
        self._seed_leg_cache()
        TripActivity.objects.filter(pk=self.act_b.pk).update(location_hidden=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "trip-activity-leg")

    def test_external_apis_disabled_skips_legs_but_cached_values_never_render_either(self) -> None:
        self._seed_leg_cache()
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "trip-activity-leg")
