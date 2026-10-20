"""Tests for the web-search entry point and its provider choice."""

from __future__ import annotations

from typing import Any

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import WEB_SEARCH_PROVIDERS
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.search.search import WebSearchProvider, search_web


class _FakeSearch(WebSearchProvider):
    """Answers with ``results`` or raises ``failure``; remembers each query."""

    def __init__(
        self,
        key: str,
        results: list[dict[str, Any]] | None = None,
        *,
        failure: Exception | None = None,
        is_available: bool = True,
    ) -> None:
        self.key = key
        self.results = results or []
        self.failure = failure
        self.is_available = is_available
        self.queries: list[tuple[str, int]] = []

    def available(self) -> bool:
        return self.is_available

    def search_web(self, query: str, *, max_results: int = 10, scope: object = None) -> list[dict[str, Any]]:
        self.queries.append((query, max_results))
        if self.failure is not None:
            raise self.failure
        return self.results


class SearchWebProviderTests(SimpleTestCase):
    """search_web() answers from the first available provider."""

    def test_returns_the_first_available_providers_results(self) -> None:
        mock_results = [{"title": "T", "link": "http://x.com", "snippet": "s"}]
        skipped = _FakeSearch("skipped", [{"title": "no"}], is_available=False)
        first = _FakeSearch("first", mock_results)
        second = _FakeSearch("second", [{"title": "later"}])
        with WEB_SEARCH_PROVIDERS.overridden([skipped, first, second]):
            results = search_web("abandoned hospital", max_results=7)

        self.assertEqual(results, mock_results)
        self.assertEqual(first.queries, [("abandoned hospital", 7)])
        self.assertEqual((skipped.queries, second.queries), ([], []))

    def test_an_outage_raises_rather_than_answering_no_results(self) -> None:
        """``[]`` here is cached as "no results" for the whole cache term."""
        failing = _FakeSearch("failing", failure=ProviderUnavailableError("every source failed"))
        with WEB_SEARCH_PROVIDERS.overridden([failing]), self.assertRaises(ProviderUnavailableError):
            search_web("query")

    def test_an_outage_is_not_handed_to_the_next_provider(self) -> None:
        failing = _FakeSearch("failing", failure=ProviderUnavailableError("every source failed"))
        later = _FakeSearch("later", [{"title": "later"}])
        with WEB_SEARCH_PROVIDERS.overridden([failing, later]), self.assertRaises(ProviderUnavailableError):
            search_web("query")

        self.assertEqual(later.queries, [])

    def test_a_rejected_query_is_no_results(self) -> None:
        class RejectedError(ProviderUnavailableError):
            reason = "invalid_query"

            @property
            def is_outage(self) -> bool:
                return False

        with WEB_SEARCH_PROVIDERS.overridden([_FakeSearch("strict", failure=RejectedError("q is required"))]):
            results = search_web("query")

        self.assertEqual(results, [])


class SearchWebWithoutAProviderTests(SimpleTestCase):
    """There is no built-in provider: with none available there are no results."""

    def test_returns_empty_list_when_no_provider_is_available(self) -> None:
        off = _FakeSearch("off", [{"title": "no"}], is_available=False)
        with WEB_SEARCH_PROVIDERS.overridden([off]):
            self.assertEqual(search_web("query"), [])

        self.assertEqual(off.queries, [])

    def test_returns_empty_list_when_there_are_no_providers(self) -> None:
        with WEB_SEARCH_PROVIDERS.overridden([]):
            self.assertEqual(search_web("query"), [])


class FormatSearchDateTests(SimpleTestCase):
    """format_search_date() needs no provider - smoke-test it."""

    def test_blank_input_returns_empty_string(self) -> None:
        from urbanlens.dashboard.services.search.search import format_search_date

        self.assertEqual(format_search_date(None), "")
        self.assertEqual(format_search_date(""), "")

    def test_unparseable_string_is_returned_as_is(self) -> None:
        from urbanlens.dashboard.services.search.search import format_search_date

        self.assertEqual(format_search_date("not-a-date"), "not-a-date")
