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


# ---------------------------------------------------------------------------
# get_search_gateways / search_web - automatic fallback chain
# ---------------------------------------------------------------------------

class GetSearchGatewaysOrderTests(TestCase):
    """get_search_gateways() returns providers in the expected fallback order."""

    def test_default_order_prioritizes_searxng_then_google_then_brave(self):
        from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchGateway
        from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchGateway
        from urbanlens.dashboard.services.apis.search.searxng import SearxngGateway
        from urbanlens.dashboard.services.search import get_search_gateways

        mock_settings = MagicMock()
        mock_settings.search_provider = ""

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateways = get_search_gateways()

        self.assertIsInstance(gateways[0], SearxngGateway)
        self.assertIsInstance(gateways[1], GoogleCustomSearchGateway)
        self.assertIsInstance(gateways[2], BraveSearchGateway)
        self.assertEqual(len(gateways), 6)

    def test_preferred_provider_is_promoted_to_the_front(self):
        from urbanlens.dashboard.services.apis.search.marginalia import MarginaliaGateway
        from urbanlens.dashboard.services.search import get_search_gateways

        mock_settings = MagicMock()
        mock_settings.search_provider = "marginalia"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateways = get_search_gateways()

        self.assertIsInstance(gateways[0], MarginaliaGateway)
        self.assertEqual(len(gateways), 6)

    def test_preferred_provider_is_not_duplicated(self):
        from urbanlens.dashboard.services.apis.search.searxng import SearxngGateway
        from urbanlens.dashboard.services.search import get_search_gateways

        mock_settings = MagicMock()
        mock_settings.search_provider = "searxng"

        with patch("urbanlens.dashboard.models.site_settings.SiteSettings") as MockSiteSettings:
            MockSiteSettings.get_current.return_value = mock_settings
            gateways = get_search_gateways()

        self.assertIsInstance(gateways[0], SearxngGateway)
        self.assertEqual(len(gateways), 6)


class SearchWebFallbackTests(TestCase):
    """search_web() tries each gateway in order until one succeeds."""

    def test_returns_first_successful_providers_results(self):
        from urbanlens.dashboard.services.search import search_web

        gw1, gw2 = MagicMock(), MagicMock()
        gw1.search.side_effect = RuntimeError("not configured")
        gw2.search.return_value = [{"title": "ok"}]

        with patch("urbanlens.dashboard.services.search.get_search_gateways", return_value=[gw1, gw2]):
            results = search_web("query")

        self.assertEqual(results, [{"title": "ok"}])
        gw1.search.assert_called_once()
        gw2.search.assert_called_once()

    def test_stops_at_the_first_successful_provider(self):
        from urbanlens.dashboard.services.search import search_web

        gw1, gw2 = MagicMock(), MagicMock()
        gw1.search.return_value = [{"title": "first"}]

        with patch("urbanlens.dashboard.services.search.get_search_gateways", return_value=[gw1, gw2]):
            results = search_web("query")

        self.assertEqual(results, [{"title": "first"}])
        gw2.search.assert_not_called()

    def test_reraises_the_last_error_when_every_provider_fails(self):
        from urbanlens.dashboard.services.search import search_web

        gw1, gw2 = MagicMock(), MagicMock()
        gw1.search.side_effect = RuntimeError("first failed")
        gw2.search.side_effect = ValueError("second failed")

        with patch("urbanlens.dashboard.services.search.get_search_gateways", return_value=[gw1, gw2]), self.assertRaises(ValueError):
            search_web("query")

    def test_rate_limited_provider_falls_back_to_next(self):
        from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError
        from urbanlens.dashboard.services.search import search_web

        gw1, gw2 = MagicMock(), MagicMock()
        gw1.search.side_effect = RateLimitExceededError("searxng")
        gw2.search.return_value = [{"title": "fallback"}]

        with patch("urbanlens.dashboard.services.search.get_search_gateways", return_value=[gw1, gw2]):
            results = search_web("query")

        self.assertEqual(results, [{"title": "fallback"}])
