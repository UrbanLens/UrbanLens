"""Per-user endpoints that spend an upstream or walk a user's pins are throttled (G5-3)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import resolve, reverse
from model_bakery import baker

from urbanlens.core.cache_backend import AtomicLocMemCache, CacheUnavailableError
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import maps
from urbanlens.dashboard.services.core.counters import Outage

ROUTES = {
    "map.geolocation.visits": {},
    "map.autocomplete.places": {},
    "map.places.nearby": {},
    "map.places.details": {},
    "pin.overlays.historical": {"pin_slug": "some-pin"},
    "location.wiki.overlays.historical": {"location_slug": "some-place"},
    "trips.weather": {"trip_slug": "some-trip"},
    "profile.social.verify": {},
}

#: Routes whose throttle guards an upstream's budget, so an outage must not turn them into free calls.
UPSTREAM_ROUTES = set(ROUTES) - {"map.geolocation.visits"}


class EveryRouteIsThrottledTests(TestCase):
    def test_each_route_carries_a_throttle(self) -> None:
        for name, kwargs in ROUTES.items():
            with self.subTest(route=name):
                func = resolve(reverse(name, kwargs=kwargs)).func
                self.assertTrue(getattr(func, "throttle_scope", None), f"{name} is not throttled")

    def test_each_is_charged_to_the_account(self) -> None:
        from urbanlens.dashboard.services.security import throttle

        for name, kwargs in ROUTES.items():
            with self.subTest(route=name):
                func = resolve(reverse(name, kwargs=kwargs)).func
                self.assertIn("GET" if name != "map.geolocation.visits" else "POST", func.throttle_methods)
                self.assertIs(func.throttle_identify, throttle.account_or_address)

    def test_upstream_routes_refuse_during_a_counter_outage(self) -> None:
        for name in UPSTREAM_ROUTES:
            with self.subTest(route=name):
                rate = resolve(reverse(name, kwargs=ROUTES[name])).func.throttle_rate
                self.assertEqual(rate.on_outage, Outage.REFUSE)


class TheLimitsBiteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_geolocation_pings_stop_at_the_limit(self) -> None:
        url = reverse("map.geolocation.visits")
        with mock.patch("urbanlens.dashboard.services.visits.visits.record_geolocation_pin_visits", return_value=[]):
            for _ in range(maps.GEOLOCATION_VISIT_RATE.limit):
                self.assertEqual(self.client.post(url, {"latitude": "42.6", "longitude": "-73.7"}).status_code, 200)
            self.assertEqual(self.client.post(url, {"latitude": "42.6", "longitude": "-73.7"}).status_code, 429)

    def test_another_account_keeps_its_own_budget(self) -> None:
        url = reverse("map.geolocation.visits")
        with mock.patch("urbanlens.dashboard.services.visits.visits.record_geolocation_pin_visits", return_value=[]):
            for _ in range(maps.GEOLOCATION_VISIT_RATE.limit + 1):
                self.client.post(url, {"latitude": "42.6", "longitude": "-73.7"})
            self.client.force_login(baker.make(User))
            self.assertEqual(self.client.post(url, {"latitude": "42.6", "longitude": "-73.7"}).status_code, 200)

    def test_place_lookup_refuses_while_the_counter_store_is_down(self) -> None:
        with (
            mock.patch.object(AtomicLocMemCache, "incr_window", side_effect=CacheUnavailableError("down")),
            mock.patch.object(maps.MapController, "place_details") as view,
        ):
            response = self.client.get(reverse("map.places.details"), {"place_id": "x"})
        self.assertEqual(response.status_code, 429)
        view.assert_not_called()
