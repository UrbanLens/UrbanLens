"""Tests for services.apis.locations.geocode_resolution - the choice among geocoders."""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import GEOCODERS
from urbanlens.dashboard.services.apis.locations import geocode_resolution
from urbanlens.dashboard.services.apis.locations.geocode_resolution import GeocodeProvider, NominatimGeocoder
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

_NOMINATIM = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway"
_PERMITTED = "urbanlens.dashboard.services.core.egress.direct_fallback_permitted"


class _FakeGeocoder(GeocodeProvider):
    """Answers with ``answer``, or raises ``failure``; remembers what it was asked."""

    def __init__(
        self,
        key: str,
        answer: tuple[float | None, float | None] = (None, None),
        *,
        failure: Exception | None = None,
        is_available: bool = True,
        shields: bool = False,
    ) -> None:
        self.key = key
        self.answer = answer
        self.failure = failure
        self.is_available = is_available
        self.shields_direct = shields
        self.asked: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def geocode(self, address: str) -> tuple[float | None, float | None]:
        self.asked.append(address)
        if self.failure is not None:
            raise self.failure
        return self.answer


class GeocodeAddressTests(SimpleTestCase):
    def test_the_first_available_geocoder_answers(self) -> None:
        skipped = _FakeGeocoder("skipped", (9.0, 9.0), is_available=False)
        first = _FakeGeocoder("first", (42.6, -73.6))
        second = _FakeGeocoder("second", (1.0, 1.0))
        with GEOCODERS.overridden([skipped, first, second]):
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))
        self.assertEqual((skipped.asked, second.asked), ([], []))

    def test_no_result_is_the_answer_and_the_next_geocoder_is_not_asked(self) -> None:
        first = _FakeGeocoder("first")
        second = _FakeGeocoder("second", (1.0, 1.0))
        with GEOCODERS.overridden([first, second]):
            result = geocode_resolution.geocode_address("nowhere")

        self.assertEqual(result, (None, None))
        self.assertEqual(second.asked, [])

    def test_no_geocoder_means_no_result(self) -> None:
        with GEOCODERS.overridden([]):
            self.assertEqual(geocode_resolution.geocode_address("1 Main St"), (None, None))

    def test_an_unavailable_geocoder_hands_over_to_the_next(self) -> None:
        failing = _FakeGeocoder("failing", failure=ProviderUnavailableError("boom"))
        fallback = _FakeGeocoder("fallback", (42.6, -73.6))
        with GEOCODERS.overridden([failing, fallback]):
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_a_shielding_geocoder_that_fails_off_production_ends_the_chain(self) -> None:
        shielding = _FakeGeocoder("shielding", failure=ProviderUnavailableError("boom"), shields=True)
        fallback = _FakeGeocoder("fallback", (42.6, -73.6))
        with (
            GEOCODERS.overridden([shielding, fallback]),
            mock.patch(_PERMITTED, return_value=False),
            pytest.raises(ProviderUnavailableError),
        ):
            geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(fallback.asked, [])

    def test_a_shielding_geocoder_that_fails_where_the_direct_fallback_is_permitted_hands_over(self) -> None:
        shielding = _FakeGeocoder("shielding", failure=ProviderUnavailableError("boom"), shields=True)
        fallback = _FakeGeocoder("fallback", (42.6, -73.6))
        with GEOCODERS.overridden([shielding, fallback]), mock.patch(_PERMITTED, return_value=True):
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_a_rate_limit_refusal_propagates(self) -> None:
        """A caller must be able to tell "we did not ask" from "no such place"."""
        with GEOCODERS.overridden([NominatimGeocoder()]), mock.patch(_NOMINATIM) as gateway_cls:
            gateway_cls.return_value.search.side_effect = RateLimitExceededError("nominatim")
            with pytest.raises(RateLimitExceededError):
                geocode_resolution.geocode_address("1 Main St")


class NominatimGeocoderTests(SimpleTestCase):
    """The direct geocoder is NominatimGateway, not a raw geopy client - see test_nominatim_fallback.py."""

    def test_it_is_always_available_and_shields_nothing(self) -> None:
        self.assertTrue(NominatimGeocoder().available())
        self.assertFalse(NominatimGeocoder.shields_direct)

    def test_it_reads_the_first_results_coordinates(self) -> None:
        with GEOCODERS.overridden([NominatimGeocoder()]), mock.patch(_NOMINATIM) as gateway_cls:
            gateway_cls.return_value.search.return_value = [{"lat": "42.6", "lon": "-73.6"}]
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_no_results_is_none_none(self) -> None:
        with GEOCODERS.overridden([NominatimGeocoder()]), mock.patch(_NOMINATIM) as gateway_cls:
            gateway_cls.return_value.search.return_value = []
            self.assertEqual(geocode_resolution.geocode_address("nowhere"), (None, None))
