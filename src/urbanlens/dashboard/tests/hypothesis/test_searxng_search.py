"""Tests for SearxngGateway.

All HTTP is mocked - no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings as hyp_settings, strategies as st
from requests import HTTPError

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.search.searxng import SearxngError, SearxngGateway

_hyp = hyp_settings(max_examples=50, deadline=None)


def _make_gw(base_url: str | None = "https://searx.example.com") -> SearxngGateway:
    gw = object.__new__(SearxngGateway)
    object.__setattr__(gw, "base_url", base_url)
    object.__setattr__(gw, "session", MagicMock())
    return gw


class SearxngValidateTests(TestCase):
    """search() raises SearxngError when no instance is configured."""

    def test_none_base_url_raises(self):
        with self.assertRaises(SearxngError):
            _make_gw(None).search("test")

    def test_empty_base_url_raises(self):
        with self.assertRaises(SearxngError):
            _make_gw("").search("test")

    def test_error_message_references_env_var(self):
        with self.assertRaises(SearxngError) as ctx:
            _make_gw(None)._validate()
        self.assertIn("UL_SEARXNG_BASE_URL", str(ctx.exception))


class SearxngParseTests(TestCase):
    """_parse converts the SearXNG JSON structure to normalised dicts."""

    def setUp(self):
        self.gw = _make_gw()

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(self.gw._parse({}), [])

    def test_single_result_extracts_fields(self):
        data = {"results": [{"title": "Abandoned Mill", "url": "http://x.com", "content": "desc"}]}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["title"], "Abandoned Mill")
        self.assertEqual(result[0]["link"], "http://x.com")
        self.assertEqual(result[0]["snippet"], "desc")

    def test_missing_fields_default_to_none(self):
        data = {"results": [{}]}
        result = self.gw._parse(data)
        self.assertIsNone(result[0]["title"])
        self.assertIsNone(result[0]["link"])
        self.assertIsNone(result[0]["snippet"])

    @given(st.lists(st.text(min_size=1, max_size=40), min_size=0, max_size=20))
    @_hyp
    def test_parse_count_matches_results_count(self, titles: list[str]):
        items = [{"title": t, "url": "http://x.com", "content": "d"} for t in titles]
        result = self.gw._parse({"results": items})
        self.assertEqual(len(result), len(titles))


class SearxngHTTPTests(TestCase):
    """search() sends the correct request, honours max_results, and handles HTTP errors."""

    def _gw_with_response(self, status: int = 200, body: dict | None = None) -> tuple[SearxngGateway, MagicMock]:
        gw = _make_gw()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = body or {}
        if status >= 400:
            mock_resp.raise_for_status.side_effect = HTTPError(f"{status} error")
        else:
            mock_resp.raise_for_status.return_value = None
        gw.session.get.return_value = mock_resp
        return gw, mock_resp

    def test_passes_query_as_q_param(self):
        gw, _ = self._gw_with_response()
        gw.search("abandoned hospital")
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["params"]["q"], "abandoned hospital")

    def test_requests_json_format(self):
        gw, _ = self._gw_with_response()
        gw.search("test")
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["params"]["format"], "json")

    def test_hits_search_path_under_base_url(self):
        gw, _ = self._gw_with_response()
        gw.search("test")
        called_url = gw.session.get.call_args[0][0]
        self.assertEqual(called_url, "https://searx.example.com/search")

    def test_truncates_to_max_results(self):
        items = [{"title": f"T{i}", "url": "http://x.com", "content": "d"} for i in range(10)]
        gw, _ = self._gw_with_response(body={"results": items})
        result = gw.search("test", max_results=3)
        self.assertEqual(len(result), 3)

    def test_500_raises_searxng_error(self):
        gw, _ = self._gw_with_response(status=500)
        with self.assertRaises(SearxngError):
            gw.search("test")

    def test_successful_search_returns_list(self):
        body = {"results": [{"title": "T", "url": "http://x.com", "content": "d"}]}
        gw, _ = self._gw_with_response(body=body)
        result = gw.search("test")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)


class SearxngRateLimitDefaultsTests(TestCase):
    """SearXNG is self-hosted infrastructure, not a metered third-party quota."""

    def test_no_daily_cap(self):
        from urbanlens.dashboard.plugins.builtin.searxng import SearxngPlugin

        defaults = SearxngPlugin().get_service_defaults()["searxng"]
        self.assertIsNone(defaults.calls_per_day)

    def test_per_minute_limit_still_protects_upstream_engines(self):
        from urbanlens.dashboard.plugins.builtin.searxng import SearxngPlugin

        defaults = SearxngPlugin().get_service_defaults()["searxng"]
        self.assertIsNotNone(defaults.calls_per_minute)
        self.assertGreater(defaults.calls_per_minute, 0)
