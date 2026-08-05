"""Tests for services.apis.locations.geocode_resolution - the REData-vs-direct-Nominatim choice."""

from __future__ import annotations

from unittest import mock

from geopy.exc import GeocoderUnavailable

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import geocode_resolution
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, LocationContextUnavailableError

_MODULE = "urbanlens.dashboard.services.apis.locations.geocode_resolution"


class GeocodeAddressTests(SimpleTestCase):
    def test_redata_configured_uses_first_result(self) -> None:
        envelope = LocationContextEnvelope(count=1, complete=True, results=[{"provider": "nominatim", "latitude": 42.6, "longitude": -73.6}])
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
        fake_location = mock.Mock(latitude=42.6, longitude=-73.6)
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch("geopy.geocoders.Nominatim.geocode", return_value=fake_location),
        ):
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_redata_failure_falls_back_to_direct_nominatim(self) -> None:
        fake_location = mock.Mock(latitude=42.6, longitude=-73.6)
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataGeocodeGateway") as gateway_cls,
            mock.patch("geopy.geocoders.Nominatim.geocode", return_value=fake_location),
        ):
            gateway_cls.return_value.geocode.side_effect = LocationContextUnavailableError("source_error", "boom")
            result = geocode_resolution.geocode_address("1 Main St")

        self.assertEqual(result, (42.6, -73.6))

    def test_direct_nominatim_exception_propagates(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch("geopy.geocoders.Nominatim.geocode", side_effect=GeocoderUnavailable("down")),
        ):
            with self.assertRaises(GeocoderUnavailable):
                geocode_resolution.geocode_address("1 Main St")
