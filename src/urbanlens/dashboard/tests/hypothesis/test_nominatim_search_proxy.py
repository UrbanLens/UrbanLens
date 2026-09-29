"""The map search reaches Nominatim only through the server, under its cache, throttle and deadline."""

from __future__ import annotations

import math
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.map_pins.autocomplete import parse_viewbox

_RAW_PLACE = {
    "lat": "40.7128",
    "lon": "-74.0060",
    "display_name": "Old Mill, Main Street, Springfield, NY",
    "namedetails": {"name": "Old Mill"},
    "address": {"road": "Main Street", "postcode": "12345"},
    "extratags": {"website": "https://example.com", "phone": "555-0100"},
    "osm_type": "way",
    "osm_id": 42,
    "importance": 0.5,
}


def _place(**overrides: object) -> dict:
    return {**NominatimGateway._normalise(_RAW_PLACE), **overrides}


class _NominatimCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.apis.request_upstreams import NominatimSearchUpstream

        NominatimSearchUpstream.reset()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _search(self, **params: str):
        return self.client.get(reverse("map.autocomplete.nominatim"), params)

    def _patch_search(self, **kwargs):
        return mock.patch.object(NominatimGateway, "search", autospec=True, **kwargs)


class NominatimProxyTests(_NominatimCase):
    def test_login_is_required(self) -> None:
        self.client.logout()
        with self._patch_search(return_value=[_place()]) as search:
            response = self._search(q="old mill")

        self.assertEqual(response.status_code, 302)
        search.assert_not_called()

    def test_results_carry_only_what_the_search_bar_uses(self) -> None:
        with self._patch_search(return_value=[_place()]):
            response = self._search(q="old mill")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [
                {
                    "lat": 40.7128,
                    "lon": -74.006,
                    "name": "Old Mill",
                    "display_name": "Old Mill, Main Street, Springfield, NY",
                }
            ],
        )

    def test_a_result_without_usable_coordinates_is_dropped(self) -> None:
        with self._patch_search(return_value=[_place(lat=None), _place(lon="east"), _place()]):
            response = self._search(q="old mill")

        self.assertEqual(len(response.json()["results"]), 1)

    def test_limit_is_clamped(self) -> None:
        cases = {"50": 10, "0": 1, "-3": 1, "7": 7, "many": 5, None: 5}
        for index, (raw, expected) in enumerate(cases.items()):
            with self.subTest(limit=raw), self._patch_search(return_value=[]) as search:
                params = {"q": f"query {index}"} | ({"limit": raw} if raw is not None else {})
                self._search(**params)
                self.assertEqual(search.call_args.kwargs["limit"], expected)

    def test_a_malformed_viewbox_is_rejected_without_asking_nominatim(self) -> None:
        for viewbox in ("1,2,3", "1,2,3,4,5", "a,b,c,d", "nan,1,2,3", "1,inf,2,3", "1;2;3;4"):
            with self.subTest(viewbox=viewbox), self._patch_search(return_value=[]) as search:
                response = self._search(q="old mill", viewbox=viewbox)
                self.assertEqual(response.status_code, 400)
                search.assert_not_called()

    def test_a_viewbox_is_passed_to_nominatim_unbounded(self) -> None:
        with self._patch_search(return_value=[]) as search:
            self._search(q="old mill", viewbox="-74.5,40.2,-73.5,41.2")

        kwargs = search.call_args.kwargs
        self.assertEqual([float(v) for v in kwargs["viewbox"].split(",")], [-74.5, 40.2, -73.5, 41.2])
        self.assertEqual(kwargs["bounded"], 0)

    def test_a_repeated_query_is_answered_from_the_cache(self) -> None:
        with self._patch_search(return_value=[_place()]) as search:
            first = self._search(q="Old  Mill", limit="3")
            second = self._search(q="old mill", limit="3")

        self.assertEqual(search.call_count, 1)
        self.assertEqual(second.json()["results"], first.json()["results"])

    def test_the_cache_key_includes_limit_and_viewbox(self) -> None:
        with self._patch_search(return_value=[_place()]) as search:
            self._search(q="old mill")
            self._search(q="old mill", limit="1")
            self._search(q="old mill", viewbox="-74.5,40.2,-73.5,41.2")
            self._search(q="old mill", viewbox="-74.50,40.20,-73.50,41.20")

        self.assertEqual(search.call_count, 3)

    def test_an_empty_answer_is_not_cached(self) -> None:
        """The gateway reports a failed request as an empty list, so an empty answer may be a failure."""
        with self._patch_search(side_effect=[[], [_place()]]) as search:
            self._search(q="old mill")
            retried = self._search(q="old mill")

        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(retried.json()["results"]), 1)

    def test_a_rate_limited_gateway_is_an_empty_unavailable_answer_and_is_not_cached(self) -> None:
        with self._patch_search(side_effect=[RateLimitExceededError("nominatim"), [_place()]]) as search:
            refused = self._search(q="old mill")
            retried = self._search(q="old mill")

        self.assertNotEqual(refused.status_code, 500)
        self.assertGreaterEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["results"], [])
        self.assertIn("unavailable", refused.json())
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(len(retried.json()["results"]), 1)
        self.assertEqual(search.call_count, 2)

    def test_a_short_query_does_not_ask_nominatim(self) -> None:
        with self._patch_search(return_value=[_place()]) as search:
            response = self._search(q=" a ")

        self.assertEqual(response.json()["results"], [])
        search.assert_not_called()

    def test_a_profile_that_turned_off_external_services_is_not_geocoded(self) -> None:
        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with self._patch_search(return_value=[_place()]) as search:
            response = self._search(q="old mill")

        self.assertEqual(response.json(), {"results": [], "source": "nominatim", "disabled": True})
        search.assert_not_called()


class ParseViewboxTests(SimpleTestCase):
    @hyp_settings(suppress_health_check=[HealthCheck.too_slow])
    @given(st.tuples(*[st.floats(allow_nan=False, allow_infinity=False, width=32)] * 4))
    def test_four_finite_numbers_are_accepted(self, values: tuple[float, float, float, float]) -> None:
        parsed = parse_viewbox(",".join(repr(v) for v in values))

        assert parsed is not None
        self.assertTrue(all(math.isclose(a, b) for a, b in zip(parsed, values, strict=True)))

    def test_blank_is_no_viewbox(self) -> None:
        self.assertIsNone(parse_viewbox(None))
        self.assertIsNone(parse_viewbox("  "))
