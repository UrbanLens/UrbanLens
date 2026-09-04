"""Tests for RedataSearchGateway against REData's ``/search/web/``/``/search/news/``
contract (``../REData/docs/api-reference.md``, "GET /search/web/" and "GET
/search/news/").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_search_gateway import (
    RedataNewsSearchGateway,
    RedataSearchGateway,
)


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock, cls: type[RedataSearchGateway] = RedataSearchGateway) -> RedataSearchGateway:
    return cls(base_url="https://redata.example.test", api_key="test-key", session=session)


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

        results = _gateway(session).search_news("query")

        self.assertEqual(results[0]["title"], "T")
        self.assertEqual(results[0]["date"], "20240105T120000Z")

    def test_503_raises_location_context_unavailable(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "rate_limited", "message": "back off"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).search_news("query")
        self.assertEqual(ctx.value.reason, "rate_limited")

    def test_news_gateway_hits_same_endpoint(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "results": []})

        _gateway(session, cls=RedataNewsSearchGateway).search_news("query")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/search/news/")
