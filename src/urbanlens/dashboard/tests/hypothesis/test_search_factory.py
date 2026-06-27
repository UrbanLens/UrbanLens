"""Tests for the search provider factory and build_pin_search_query.

Neither test class hits the database; all pin data is supplied via mocks.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.search import build_pin_search_query
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

_hyp = hyp_settings(max_examples=60, deadline=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pin(
    *,
    effective_name: str | None = None,
    address_basic: str | None = None,
    place_name: str | None = None,
    route: str | None = None,
    city: str | None = None,
    county: str | None = None,
    state: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> MagicMock:
    """Build a mock Pin with the given field values.

    Sets effective_latitude/effective_longitude (the computed attributes the
    function reads) and wires pin.location so route doesn't become a MagicMock.
    """
    pin = MagicMock()
    pin.effective_name = effective_name
    pin.address_basic = address_basic
    pin.place_name = place_name
    pin.city = city
    pin.county = county
    pin.state = state
    pin.effective_latitude = latitude
    pin.effective_longitude = longitude

    if route is not None:
        location_mock = MagicMock()
        location_mock.route = route
        pin.location = location_mock
    else:
        pin.location = None

    return pin


# ---------------------------------------------------------------------------
# build_pin_search_query
# ---------------------------------------------------------------------------

class BuildPinSearchQueryNameTests(TestCase):
    """effective_name is always the first term when present."""

    def test_name_only(self):
        result = build_pin_search_query(_pin(effective_name="Riverside Mill"))
        self.assertEqual(result, "Riverside Mill")

    def test_name_and_state(self):
        result = build_pin_search_query(_pin(effective_name="Old Hospital", state="OH"))
        self.assertIn("Old Hospital", result)
        self.assertIn("OH", result)
        self.assertTrue(result.startswith("Old Hospital"))

    def test_name_and_city_and_state(self):
        result = build_pin_search_query(_pin(effective_name="Steel Works", city="Pittsburgh", state="PA"))
        self.assertIn("Steel Works", result)
        self.assertIn("Pittsburgh", result)
        self.assertIn("PA", result)

    def test_address_added_when_different_from_name(self):
        result = build_pin_search_query(_pin(effective_name="The Mill", address_basic="123 Mill Rd"))
        self.assertIn("123 Mill Rd", result)

    def test_address_not_repeated_when_same_as_name(self):
        result = build_pin_search_query(_pin(effective_name="123 Mill Rd", address_basic="123 Mill Rd"))
        self.assertEqual(result.count("123 Mill Rd"), 1)

    def test_place_name_not_repeated_when_same_as_name(self):
        result = build_pin_search_query(_pin(effective_name="River Mill", place_name="River Mill", state="OH"))
        self.assertEqual(result.count("River Mill"), 1)

    def test_place_name_not_repeated_when_same_as_address(self):
        result = build_pin_search_query(_pin(
            effective_name="Mill",
            address_basic="100 Main St",
            place_name="100 Main St",
        ))
        self.assertEqual(result.count("100 Main St"), 1)

    def test_place_name_included_when_unique(self):
        result = build_pin_search_query(_pin(
            effective_name="Factory",
            address_basic="1 Industrial Ave",
            place_name="Old Iron Works",
            city="Detroit",
            state="MI",
        ))
        self.assertIn("Old Iron Works", result)

    def test_county_used_when_city_absent(self):
        result = build_pin_search_query(_pin(effective_name="Farm", county="Erie County"))
        self.assertIn("Erie County", result)

    def test_city_preferred_over_county(self):
        result = build_pin_search_query(_pin(effective_name="Farm", city="Cleveland", county="Cuyahoga County"))
        self.assertIn("Cleveland", result)
        self.assertNotIn("Cuyahoga County", result)

    def test_no_data_and_no_coords_returns_empty_string(self):
        result = build_pin_search_query(_pin())
        self.assertEqual(result, "")

    def test_no_data_with_coords_returns_lat_lng(self):
        result = build_pin_search_query(_pin(latitude=40.7, longitude=-74.0))
        self.assertEqual(result, "40.7, -74.0")

    def test_coords_not_used_when_other_data_present(self):
        result = build_pin_search_query(_pin(effective_name="Mill", latitude=40.7, longitude=-74.0))
        self.assertNotIn("40.7", result)

    def test_output_is_comma_separated(self):
        result = build_pin_search_query(_pin(effective_name="Mill", city="Akron", state="OH"))
        parts = [p.strip() for p in result.split(",")]
        self.assertIn("Mill", parts)
        self.assertIn("Akron", parts)
        self.assertIn("OH", parts)

    def test_no_leading_or_trailing_commas(self):
        result = build_pin_search_query(_pin(effective_name="Mill", state="OH"))
        self.assertFalse(result.startswith(","))
        self.assertFalse(result.endswith(","))

    def test_meaningless_name_omitted_from_query(self):
        result = build_pin_search_query(_pin(effective_name="Abandoned", state="OH"))
        self.assertNotIn("Abandoned", result)
        self.assertIn("OH", result)

    def test_coordinate_name_omitted_from_query(self):
        result = build_pin_search_query(_pin(effective_name="40.7, -74.0", city="Akron", state="OH"))
        self.assertNotIn("40.7", result)
        self.assertIn("Akron", result)

    def test_meaningless_place_name_omitted_from_query(self):
        result = build_pin_search_query(_pin(
            effective_name="Old Mill",
            place_name="No Information Available",
            state="OH",
        ))
        self.assertEqual(result.count("Old Mill"), 1)
        self.assertNotIn("No Information Available", result)

    @given(
        name=st.text(min_size=1, max_size=40).filter(is_meaningful_name),
        state=st.text(min_size=1, max_size=10),
    )
    @_hyp
    def test_result_contains_name_and_state(self, name: str, state: str):
        result = build_pin_search_query(_pin(effective_name=name, state=state))
        self.assertIn(name, result)
        self.assertIn(state, result)

    @given(
        lat=st.floats(min_value=-90, max_value=90, allow_nan=False),
        lng=st.floats(min_value=-180, max_value=180, allow_nan=False),
    )
    @_hyp
    def test_coords_fallback_contains_both_values(self, lat: float, lng: float):
        result = build_pin_search_query(_pin(latitude=lat, longitude=lng))
        self.assertIn(str(lat), result)
        self.assertIn(str(lng), result)


# ---------------------------------------------------------------------------
# get_search_gateway - factory
# ---------------------------------------------------------------------------

class GetSearchGatewayTests(TestCase):
    """get_search_gateway returns the correct gateway for each provider."""

    def test_brave_provider_returns_brave_gateway(self):
        from urbanlens.dashboard.services.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "brave"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, BraveSearchGateway)

    def test_google_provider_returns_google_gateway(self):
        from urbanlens.dashboard.services.google.search import GoogleCustomSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "google"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, GoogleCustomSearchGateway)

    def test_db_error_falls_back_to_brave(self):
        from urbanlens.dashboard.services.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.side_effect = Exception("DB unavailable")
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, BraveSearchGateway)

    def test_returned_gateway_satisfies_search_gateway_protocol(self):
        from urbanlens.dashboard.services.search import SearchGateway, get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "brave"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, SearchGateway)

    def test_unknown_provider_defaults_to_brave(self):
        from urbanlens.dashboard.services.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "nonexistent_provider"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, BraveSearchGateway)
