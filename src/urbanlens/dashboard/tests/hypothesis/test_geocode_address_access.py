"""The settings geocode endpoint spends paid and shared upstream budgets, so only a signed-in profile, within its own
budget, may reach them."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.settings import GEOCODE_UPSTREAM_RATE

_URL = "/dashboard/settings/geocode/"
_GOOGLE = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway"
_NOMINATIM = "urbanlens.dashboard.services.apis.locations.geocode_resolution.nominatim_geocode"


class GeocodeAddressAccessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def _upstreams(self):
        google = patch(_GOOGLE)
        nominatim = patch(_NOMINATIM, return_value=(None, None))
        return google, nominatim

    def test_an_anonymous_caller_never_reaches_an_upstream(self) -> None:
        google, nominatim = self._upstreams()
        with google as google_cls, nominatim as nominatim_fn:
            response = self.client.get(_URL, {"address": "Albany, NY"})

        self.assertNotEqual(response.status_code, 200)
        google_cls.return_value.geocode_place_name.assert_not_called()
        nominatim_fn.assert_not_called()

    def test_a_profile_cannot_spend_past_its_budget(self) -> None:
        self.client.force_login(baker.make(User))
        google, nominatim = self._upstreams()
        with google as google_cls, nominatim:
            google_cls.return_value.geocode_place_name.return_value = {"results": []}
            for _ in range(GEOCODE_UPSTREAM_RATE.limit):
                self.client.get(_URL, {"address": "Albany, NY"})
            calls_within_budget = google_cls.return_value.geocode_place_name.call_count

            response = self.client.get(_URL, {"address": "Albany, NY"})

        self.assertEqual(calls_within_budget, GEOCODE_UPSTREAM_RATE.limit)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertIn("error", response.json())
        self.assertEqual(google_cls.return_value.geocode_place_name.call_count, GEOCODE_UPSTREAM_RATE.limit)

    def test_coordinate_lookups_do_not_spend_the_budget(self) -> None:
        self.client.force_login(baker.make(User))
        for _ in range(GEOCODE_UPSTREAM_RATE.limit + 1):
            self.assertEqual(self.client.get(_URL, {"address": "42.65, -73.75"}).status_code, 200)

        google, nominatim = self._upstreams()
        with google as google_cls, nominatim:
            google_cls.return_value.geocode_place_name.return_value = {"results": []}
            response = self.client.get(_URL, {"address": "Albany, NY"})

        self.assertEqual(response.status_code, 404)

    def test_one_profile_spending_its_budget_leaves_another_untouched(self) -> None:
        google, nominatim = self._upstreams()
        with google as google_cls, nominatim:
            google_cls.return_value.geocode_place_name.return_value = {"results": []}
            self.client.force_login(baker.make(User))
            for _ in range(GEOCODE_UPSTREAM_RATE.limit + 1):
                self.client.get(_URL, {"address": "Albany, NY"})
            self.client.force_login(baker.make(User))

            response = self.client.get(_URL, {"address": "Albany, NY"})

        self.assertEqual(response.status_code, 404)
