"""Tests for the search provider factory.

Neither test class hits the database; all pin data is supplied via mocks.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
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
# get_search_gateway - factory
# ---------------------------------------------------------------------------

class GetSearchGatewayTests(TestCase):
    """get_search_gateway returns the correct gateway for each provider."""

    def test_brave_provider_returns_brave_gateway(self):
        from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "brave"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, BraveSearchGateway)

    def test_google_provider_returns_google_gateway(self):
        from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "google"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, GoogleCustomSearchGateway)

    def test_db_error_falls_back_to_brave(self):
        from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchGateway
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
        from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.search import get_search_gateway

        mock_settings = MagicMock()
        mock_settings.search_provider = "nonexistent_provider"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateway = get_search_gateway()

        self.assertIsInstance(gateway, BraveSearchGateway)
