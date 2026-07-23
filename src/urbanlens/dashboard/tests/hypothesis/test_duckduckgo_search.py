"""Tests for DuckDuckGoGateway.

All HTTP is mocked - no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from requests import HTTPError

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.search.duckduckgo import DuckDuckGoError, DuckDuckGoGateway

_hyp = hyp_settings(max_examples=50, deadline=None)


def _make_gw() -> DuckDuckGoGateway:
    gw = object.__new__(DuckDuckGoGateway)
    object.__setattr__(gw, "base_url", "https://api.duckduckgo.com/")
    object.__setattr__(gw, "session", MagicMock())
    return gw


class DuckDuckGoParseTests(SimpleTestCase):
    """_parse converts the Instant Answer JSON structure to normalised dicts."""

    def setUp(self):
        self.gw = _make_gw()

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(self.gw._parse({}), [])

    def test_abstract_becomes_first_result(self):
        data = {"Heading": "Abandoned Mill", "AbstractText": "A derelict textile mill.", "AbstractURL": "http://en.wikipedia.org/x"}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["title"], "Abandoned Mill")
        self.assertEqual(result[0]["link"], "http://en.wikipedia.org/x")
        self.assertEqual(result[0]["snippet"], "A derelict textile mill.")

    def test_abstract_without_url_is_skipped(self):
        data = {"AbstractText": "text with no source"}
        self.assertEqual(self.gw._parse(data), [])

    def test_related_topics_flat_list(self):
        data = {"RelatedTopics": [{"Text": "Some Place - a description", "FirstURL": "http://x.com/place"}]}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["title"], "Some Place")
        self.assertEqual(result[0]["snippet"], "a description")
        self.assertEqual(result[0]["link"], "http://x.com/place")

    def test_related_topics_disambiguation_group_flattened(self):
        data = {
            "RelatedTopics": [
                {
                    "Name": "Category",
                    "Topics": [{"Text": "Nested Place - desc", "FirstURL": "http://x.com/nested"}],
                },
            ],
        }
        result = self.gw._parse(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["link"], "http://x.com/nested")

    def test_related_topic_missing_url_is_skipped(self):
        data = {"RelatedTopics": [{"Text": "No URL here"}]}
        self.assertEqual(self.gw._parse(data), [])

    @given(st.lists(st.text(min_size=1, max_size=40), min_size=0, max_size=20))
    @_hyp
    def test_parse_count_matches_related_topics_with_urls(self, texts: list[str]):
        items = [{"Text": t, "FirstURL": "http://x.com"} for t in texts]
        result = self.gw._parse({"RelatedTopics": items})
        self.assertEqual(len(result), len(texts))


class DuckDuckGoHTTPTests(SimpleTestCase):
    """search() sends the correct request and handles HTTP errors."""

    def _gw_with_response(self, status: int = 200, body: dict | None = None) -> tuple[DuckDuckGoGateway, MagicMock]:
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

    def test_requests_json_format_no_html_skip_disambig(self):
        gw, _ = self._gw_with_response()
        gw.search("test")
        params = gw.session.get.call_args[1]["params"]
        self.assertEqual(params["format"], "json")
        self.assertEqual(params["no_html"], "1")
        self.assertEqual(params["skip_disambig"], "1")

    def test_no_auth_required_no_key_error_possible(self):
        gw, _ = self._gw_with_response()
        # Should not raise even though no API key concept exists for this gateway.
        gw.search("test")

    def test_500_raises_duckduckgo_error(self):
        gw, _ = self._gw_with_response(status=500)
        with self.assertRaises(DuckDuckGoError):
            gw.search("test")

    def test_successful_search_returns_list(self):
        body = {"Heading": "T", "AbstractText": "d", "AbstractURL": "http://x.com"}
        gw, _ = self._gw_with_response(body=body)
        result = gw.search("test")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
