"""Tests for RedataSearchGateway against REData's ``/search/web/``/``/search/news/`` contract (``../REData/docs/api-reference.md``, "GET /search/web/" and "GET /search/news/")."""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_search_gateway import (
    RedataNewsSearchGateway,
    RedataSearchGateway,
)
from urbanlens.dashboard.tests.hypothesis.redata_helpers import BUDGET_REFUSALS


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock, cls: type[RedataSearchGateway] = RedataSearchGateway) -> RedataSearchGateway:
    return cls(base_url="https://redata.example.test", api_key="test-key", session=session)


_ARTICLE = {
    "title": "T",
    "link": "http://x.com",
    "snippet": "example.com",
    "date": "20240105T120000Z",
    "thumbnail": None,
}


def _news_body(results: list[dict], *, complete: bool, gdelt: str, searxng: str) -> dict:
    """A ``/search/news/`` body as REData 0.3.7 sends it, with GDELT and SearXNG in the given states."""
    answered = "gdelt" if gdelt == "ok" else "searxng" if searxng == "ok" else ""
    return {
        "count": len(results),
        "complete": complete,
        "results": results,
        "provider": answered,
        "degraded": answered == "searxng",
        "providers": [
            {
                "provider": "gdelt",
                "status": gdelt,
                "count": len(results) if gdelt == "ok" else 0,
                "message": None,
                "radius_meters": None,
                "limit": 10,
            },
            {
                "provider": "searxng",
                "status": searxng,
                "count": len(results) if searxng == "ok" else 0,
                "message": None,
                "radius_meters": None,
                "limit": None,
            },
        ],
        "query": "query",
        "months": 24,
    }


class ServiceKeyTests(SimpleTestCase):
    """RedataSearchGateway and RedataNewsSearchGateway track separate service keys."""

    def test_web_search_service_key(self) -> None:
        self.assertEqual(RedataSearchGateway.service_key, "redata_search_web")

    def test_news_search_service_key_is_distinct(self) -> None:
        self.assertEqual(RedataNewsSearchGateway.service_key, "redata_search_news")

    def test_news_gateway_is_a_search_gateway(self) -> None:
        self.assertTrue(issubclass(RedataNewsSearchGateway, RedataSearchGateway))


class SearchWebTests(SimpleTestCase):
    def test_sends_query_and_limit(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session).search_web("abandoned hospital", max_results=5)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/search/web/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "abandoned hospital")
        self.assertEqual(params["limit"], 5)
        self.assertNotIn("images", params)

    def test_images_true_sends_images_param(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session).search_web("118 W 9th St", images=True)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["images"], "true")

    def test_returns_results_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "results": [{"title": "T", "link": "http://x.com", "snippet": "s", "date": None, "thumbnail": None}],
                "provider": "brave",
            },
        )

        results = _gateway(session).search_web("query")

        self.assertEqual(
            results, [{"title": "T", "link": "http://x.com", "snippet": "s", "date": None, "thumbnail": None}]
        )

    def test_missing_results_key_returns_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0})

        self.assertEqual(_gateway(session).search_web("query"), [])

    def test_non_dict_body_returns_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [1, 2, 3])

        self.assertEqual(_gateway(session).search_web("query"), [])

    def test_non_list_results_returns_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"results": "not-a-list"})

        self.assertEqual(_gateway(session).search_web("query"), [])

    def test_503_raises_location_context_unavailable(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            503, {"error": "all_providers_unavailable", "message": "every source failed"}
        )

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).search_web("query")
        self.assertEqual(ctx.value.reason, "all_providers_unavailable")

    def test_network_error_raises_location_context_unavailable(self) -> None:
        session = mock.Mock()
        session.get.side_effect = OSError("connection refused")

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).search_web("query")


class SearchNewsTests(SimpleTestCase):
    def test_sends_query_and_limit(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session).search_news("abandoned hospital", max_results=3)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/search/news/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "abandoned hospital")
        self.assertEqual(params["limit"], 3)
        self.assertNotIn("months", params)

    def test_months_is_sent_when_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session).search_news("query", months=6)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["months"], 6)

    def test_returns_results_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "results": [
                    {
                        "title": "T",
                        "link": "http://x.com",
                        "snippet": "example.com",
                        "date": "20240105T120000Z",
                        "thumbnail": None,
                    }
                ],
            },
        )

        results = _gateway(session).search_news("query").results

        self.assertEqual(results[0]["title"], "T")
        self.assertEqual(results[0]["date"], "20240105T120000Z")

    def test_an_answer_from_a_redata_that_sends_no_completeness_is_complete(self) -> None:
        """REData before 0.3.7 sends only ``results``, as the body above does; it is read as it always was."""
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": [], "provider": "searxng"})

        answer = _gateway(session).search_news("query")

        self.assertTrue(answer.complete)
        self.assertEqual(answer.results, [])
        self.assertEqual(answer.unanswered_sources, [])

    def test_gdelts_own_answer_is_complete(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200, _news_body([_ARTICLE], complete=True, gdelt="ok", searxng="not_attempted")
        )

        answer = _gateway(session).search_news("query")

        self.assertTrue(answer.complete)
        self.assertEqual(answer.results, [_ARTICLE])
        self.assertEqual(answer.unanswered_sources, [])

    def test_a_fallbacks_answer_keeps_its_articles_and_names_the_primary_it_stood_in_for(self) -> None:
        for status in ("rate_limited", "key_budget_exhausted", "unavailable"):
            with self.subTest(status):
                session = mock.Mock()
                session.get.return_value = _response(
                    200, _news_body([_ARTICLE], complete=False, gdelt=status, searxng="ok")
                )

                answer = _gateway(session).search_news("query")

                self.assertFalse(answer.complete)
                self.assertEqual(answer.results, [_ARTICLE])
                self.assertEqual(answer.unanswered_sources, ["gdelt"])

    def test_a_fallbacks_empty_answer_is_not_no_news(self) -> None:
        """GDELT was not asked and SearXNG found nothing: that says nothing about the place's coverage."""
        session = mock.Mock()
        session.get.return_value = _response(200, _news_body([], complete=False, gdelt="rate_limited", searxng="ok"))

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).search_news("query")
        self.assertTrue(ctx.value.is_outage)

    def test_the_enveloped_503_still_raises(self) -> None:
        """From 0.3.7 the 503 carries the same envelope as a 200, with ``error`` and ``message`` added."""
        session = mock.Mock()
        body = {
            **_news_body([], complete=False, gdelt="rate_limited", searxng="unavailable"),
            "error": "search_unavailable",
            "message": "gdelt: rate_limited; searxng: unavailable",
        }
        session.get.return_value = _response(503, body)

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).search_news("query")
        self.assertEqual(ctx.value.reason, "search_unavailable")

    def test_503_raises_location_context_unavailable(self) -> None:
        for error in BUDGET_REFUSALS:
            with self.subTest(error):
                session = mock.Mock()
                session.get.return_value = _response(503, {"error": error, "message": "back off"})

                with pytest.raises(LocationContextUnavailableError) as ctx:
                    _gateway(session).search_news("query")
                self.assertEqual(ctx.value.reason, error)

    def test_news_gateway_hits_same_endpoint(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session, cls=RedataNewsSearchGateway).search_news("query")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/search/news/")
