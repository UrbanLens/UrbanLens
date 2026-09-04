"""Tests for the REData-backed web-search entry point.

``search_web()`` no longer runs a local provider fallback chain (SearXNG,
Brave, Mojeek, Marginalia, Google Programmable Search, DuckDuckGo) - it calls
REData's ``/search/web/``, which already implements the same kind of ordered
fallback chain server-side (see ``../REData/docs/api-reference.md``, "GET
/search/web/"). These tests cover the REData-configured and
REData-unconfigured paths; the old per-provider fallback-order tests no
longer apply now that there is only one provider (REData) to try.

Neither test class hits the database or a real network - ``RedataSearchGateway``
itself is replaced with a mock, so its real constructor (which validates
``UL_REDATA_API_URL``/``UL_REDATA_API_KEY``) never runs.
"""

from __future__ import annotations

from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

_GATEWAY_CLASS_PATH = "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway"


class SearchWebRedataConfiguredTests(SimpleTestCase):
    """search_web() delegates to RedataSearchGateway when REData is configured."""

    def test_returns_redata_gateways_results(self) -> None:
        from urbanlens.dashboard.services.search.search import search_web

        mock_results = [{"title": "T", "link": "http://x.com", "snippet": "s"}]
        with (
            patch("urbanlens.dashboard.services.search.search.redata_configured", return_value=True),
            patch(_GATEWAY_CLASS_PATH) as mock_gateway_class,
        ):
            mock_gateway_class.return_value.search_web.return_value = mock_results
            results = search_web("abandoned hospital", max_results=7)

        self.assertEqual(results, mock_results)
        mock_gateway_class.return_value.search_web.assert_called_once_with("abandoned hospital", max_results=7)

    def test_unavailable_error_degrades_to_empty_list(self) -> None:
        """No fallback exists once REData is the sole provider - an outage yields
        ``[]`` so the pin panel's existing "no results" handling degrades
        gracefully rather than surfacing an error card (see
        ``PinController._web_search_response``)."""
        from urbanlens.dashboard.services.search.search import search_web

        with (
            patch("urbanlens.dashboard.services.search.search.redata_configured", return_value=True),
            patch(_GATEWAY_CLASS_PATH) as mock_gateway_class,
        ):
            mock_gateway_class.return_value.search_web.side_effect = LocationContextUnavailableError(
                "all_providers_unavailable", "every source failed"
            )
            results = search_web("query")

        self.assertEqual(results, [])


class SearchWebRedataUnconfiguredTests(SimpleTestCase):
    """search_web() has no local fallback - an unconfigured REData means no results."""

    def test_returns_empty_list_without_contacting_redata(self) -> None:
        from urbanlens.dashboard.services.search.search import search_web

        with (
            patch("urbanlens.dashboard.services.search.search.redata_configured", return_value=False),
            patch(_GATEWAY_CLASS_PATH) as mock_gateway_class,
        ):
            results = search_web("query")

        self.assertEqual(results, [])
        mock_gateway_class.return_value.search_web.assert_not_called()


class FormatSearchDateTests(SimpleTestCase):
    """format_search_date() is untouched by the REData migration - smoke-test it."""

    def test_blank_input_returns_empty_string(self) -> None:
        from urbanlens.dashboard.services.search.search import format_search_date

        self.assertEqual(format_search_date(None), "")
        self.assertEqual(format_search_date(""), "")

    def test_unparseable_string_is_returned_as_is(self) -> None:
        from urbanlens.dashboard.services.search.search import format_search_date

        self.assertEqual(format_search_date("not-a-date"), "not-a-date")
