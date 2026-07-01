"""Tests for the geocode_address settings view.

The view accepts GET ?address=<text> and returns JSON {lat, lng}.

Invariants verified:
  - Empty or missing address returns HTTP 400.
  - A "lat, lng" string within valid geographic bounds is parsed without any
    external API call and returned exactly.
  - Out-of-range values fall through to the Google Geocoding gateway.
  - A successful Google Geocoding response is relayed as {lat, lng}.
  - A failed or empty Google Geocoding response returns HTTP 404.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, assume, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_db_settings = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_GEOCODE_URL = "/dashboard/settings/geocode/"

# Valid geographic ranges (matching the view's validation logic).
_valid_lat = st.floats(min_value=-90.0, max_value=90.0, allow_nan=False, allow_infinity=False)
_valid_lng = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False)


class GeocodeAddressEmptyInputTests(TestCase):
    """Missing or blank address must return 400."""

    def test_missing_address_param_returns_400(self) -> None:
        resp = self.client.get(_GEOCODE_URL)
        self.assertEqual(resp.status_code, 400)

    def test_empty_address_param_returns_400(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": ""})
        self.assertEqual(resp.status_code, 400)

    def test_whitespace_only_address_returns_400(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "   "})
        self.assertEqual(resp.status_code, 400)


class GeocodeAddressCoordParsingTests(TestCase):
    """'lat, lng' strings within valid bounds must be parsed without hitting Google."""

    def test_valid_lat_lng_string_returns_200(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "42.65, -73.75"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertAlmostEqual(data["lat"], 42.65, places=4)
        self.assertAlmostEqual(data["lng"], -73.75, places=4)

    def test_no_space_after_comma_is_accepted(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "42.65,-73.75"})
        self.assertEqual(resp.status_code, 200)

    def test_boundary_lat_90_is_accepted(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "90.0, 0.0"})
        self.assertEqual(resp.status_code, 200)

    def test_boundary_lat_neg90_is_accepted(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "-90.0, 0.0"})
        self.assertEqual(resp.status_code, 200)

    def test_boundary_lng_180_is_accepted(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "0.0, 180.0"})
        self.assertEqual(resp.status_code, 200)

    def test_boundary_lng_neg180_is_accepted(self) -> None:
        resp = self.client.get(_GEOCODE_URL, {"address": "0.0, -180.0"})
        self.assertEqual(resp.status_code, 200)

    def test_out_of_range_lat_does_not_short_circuit(self) -> None:
        """lat > 90 must fall through to Google (mocked here to return 404)."""
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = {"results": []}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "95.0, 0.0"})
        # No short-circuit: Google was consulted and found nothing.
        self.assertEqual(resp.status_code, 404)

    def test_out_of_range_lng_does_not_short_circuit(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = {"results": []}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "0.0, 200.0"})
        self.assertEqual(resp.status_code, 404)

    def test_non_numeric_string_falls_through_to_google(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = {"results": []}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "Albany, NY"})
        mock_cls.return_value.geocode_place_name.assert_called_once_with("Albany, NY")
        self.assertEqual(resp.status_code, 404)

    def test_three_part_string_falls_through_to_google(self) -> None:
        """Three comma-separated values must not be treated as lat/lng."""
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = {"results": []}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "1,2,3"})
        self.assertEqual(resp.status_code, 404)

    @given(lat=_valid_lat, lng=_valid_lng)
    @_db_settings
    def test_any_valid_coord_pair_is_parsed_and_returned(
        self, lat: float, lng: float,
    ) -> None:
        address = f"{lat},{lng}"
        resp = self.client.get(_GEOCODE_URL, {"address": address})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertAlmostEqual(data["lat"], lat, places=5)
        self.assertAlmostEqual(data["lng"], lng, places=5)


class GeocodeAddressGoogleFallbackTests(TestCase):
    """When parsing fails, the view must delegate to GoogleGeocodingGateway."""

    def _google_result(self, lat: float, lng: float) -> dict:
        return {"results": [{"geometry": {"location": {"lat": lat, "lng": lng}}}]}

    def test_google_success_returns_lat_lng(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls:
            mock_cls.return_value.geocode_place_name.return_value = self._google_result(42.65, -73.75)
            resp = self.client.get(_GEOCODE_URL, {"address": "Albany, NY"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertAlmostEqual(data["lat"], 42.65, places=4)
        self.assertAlmostEqual(data["lng"], -73.75, places=4)

    def test_google_empty_results_returns_404(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = {"results": []}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "Nowhere XYZ"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertIn("error", data)

    def test_google_none_result_returns_404(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.return_value = None
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "Nowhere XYZ"})
        self.assertEqual(resp.status_code, 404)

    def test_google_key_error_returns_404(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            # Malformed response missing the expected nested keys.
            mock_cls.return_value.geocode_place_name.return_value = {"results": [{"bad": "shape"}]}
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "Somewhere"})
        self.assertEqual(resp.status_code, 404)

    def test_google_value_error_returns_404(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway",
        ) as mock_cls, patch("geopy.geocoders.Nominatim") as mock_nominatim:
            mock_cls.return_value.geocode_place_name.side_effect = ValueError("API error")
            mock_nominatim.return_value.geocode.return_value = None
            resp = self.client.get(_GEOCODE_URL, {"address": "Somewhere"})
        self.assertEqual(resp.status_code, 404)
