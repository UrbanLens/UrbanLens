"""Tests for services.apis.locations.geocode_resolution - the REData-vs-direct-Nominatim choice."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import geocode_resolution
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)

_MODULE = "urbanlens.dashboard.services.apis.locations.geocode_resolution"


class GeocodeAddressTests(SimpleTestCase):
    def test_redata_configured_uses_first_result(self) -> None:
        envelope = LocationContextEnvelope(
            count=1, complete=True, results=[{"provider": "nominatim", "latitude": 42.6, "longitude": -73.6}]
        )
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataGeocodeGateway") as gateway_cls,
        ):
            gateway_cls.return_value.geocode.return_value = envelope
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_redata_configured_no_results_returns_none_none(self) -> None:
        envelope = LocationContextEnvelope(count=0, complete=True, results=[])
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataGeocodeGateway") as gateway_cls,
        ):
            gateway_cls.return_value.geocode.return_value = envelope
            result = geocode_resolution.geocode_address("nowhere")

        self.assertEqual(result, (None, None))

    def test_redata_not_configured_uses_direct_nominatim(self) -> None:
        """The fallback is NominatimGateway, not a raw geopy client - see test_nominatim_fallback.py."""
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch("urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway") as gateway_cls,
        ):
            gateway_cls.return_value.search.return_value = [{"lat": "42.6", "lon": "-73.6"}]
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_redata_failure_falls_back_to_direct_nominatim(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataGeocodeGateway") as redata_cls,
            mock.patch("urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway") as gateway_cls,
        ):
            redata_cls.return_value.geocode.side_effect = LocationContextUnavailableError("source_error", "boom")
            gateway_cls.return_value.search.return_value = [{"lat": "42.6", "lon": "-73.6"}]
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_a_rate_limit_refusal_propagates(self) -> None:
        """A caller must be able to tell "we did not ask" from "no such place"."""
        import pytest

        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch("urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway") as gateway_cls,
        ):
            gateway_cls.return_value.search.side_effect = RateLimitExceededError("nominatim")
            with pytest.raises(RateLimitExceededError):
                geocode_resolution.geocode_address("1 Main St")
