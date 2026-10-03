"""Tests for the REData-backed web-search entry point."""

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

    def test_an_outage_raises_rather_than_answering_no_results(self) -> None:
        """``[]`` here is cached as "no results" for the whole cache term (P187)."""
        from urbanlens.dashboard.services.search.search import search_web

        with (
            patch("urbanlens.dashboard.services.search.search.redata_configured", return_value=True),
            patch(_GATEWAY_CLASS_PATH) as mock_gateway_class,
        ):
            mock_gateway_class.return_value.search_web.side_effect = LocationContextUnavailableError(
                "all_providers_unavailable", "every source failed"
            )
            with self.assertRaises(LocationContextUnavailableError):
                search_web("query")

    def test_a_rejected_query_is_no_results(self) -> None:
        from urbanlens.dashboard.services.search.search import search_web

        with (
            patch("urbanlens.dashboard.services.search.search.redata_configured", return_value=True),
            patch(_GATEWAY_CLASS_PATH) as mock_gateway_class,
        ):
            mock_gateway_class.return_value.search_web.side_effect = LocationContextUnavailableError(
                "invalid_query", "q is required", rejected=True
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
