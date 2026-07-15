"""Tests for MarginaliaGateway.

All HTTP is mocked - no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from requests import HTTPError

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.search.marginalia import (
    PUBLIC_TEST_API_KEY,
    MarginaliaError,
    MarginaliaGateway,
)

_hyp = hyp_settings(max_examples=50, deadline=None)


def _make_gw(api_key: str | None = "test-key") -> MarginaliaGateway:
    gw = object.__new__(MarginaliaGateway)
    object.__setattr__(gw, "api_key", api_key)
    object.__setattr__(gw, "base_url", "https://api2.marginalia-search.com/search")
    object.__setattr__(gw, "session", MagicMock())
    return gw


class MarginaliaConfigTests(TestCase):
    """No API key is required - the shared 'public' key is documented as the fallback."""

    def test_post_init_falls_back_to_public_key(self):
        gw = MarginaliaGateway.__new__(MarginaliaGateway)
        object.__setattr__(gw, "api_key", None)
        object.__setattr__(gw, "session", MagicMock())
        from urbanlens.UrbanLens.settings.app import settings

        original = settings.marginalia_api_key
        try:
            settings.marginalia_api_key = None
            gw.__post_init__()
        finally:
            settings.marginalia_api_key = original
        self.assertEqual(gw.api_key, PUBLIC_TEST_API_KEY)


class MarginaliaParseTests(TestCase):
    """_parse converts the Marginalia JSON structure to normalised dicts."""

    def setUp(self):
        self.gw = _make_gw()

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(self.gw._parse({}), [])

    def test_single_result_extracts_fields(self):
        data = {"results": [{"title": "Abandoned Mill", "url": "http://x.com", "description": "desc"}]}
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
        items = [{"title": t, "url": "http://x.com", "description": "d"} for t in titles]
        result = self.gw._parse({"results": items})
        self.assertEqual(len(result), len(titles))


class MarginaliaHTTPTests(TestCase):
    """search() sends the correct request and handles HTTP errors."""

    def _gw_with_response(self, status: int = 200, body: dict | None = None) -> tuple[MarginaliaGateway, MagicMock]:
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

    def test_passes_query_as_query_param(self):
        gw, _ = self._gw_with_response()
        gw.search("abandoned hospital")
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["params"]["query"], "abandoned hospital")

    def test_sends_api_key_header(self):
        gw, _ = self._gw_with_response()
        gw.search("test")
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["headers"]["API-Key"], "test-key")

    def test_count_clamped_to_100(self):
        gw, _ = self._gw_with_response()
        gw.search("test", max_results=500)
        call_kwargs = gw.session.get.call_args[1]
        self.assertLessEqual(call_kwargs["params"]["count"], 100)

    def test_count_clamped_to_minimum_1(self):
        gw, _ = self._gw_with_response()
        gw.search("test", max_results=0)
        call_kwargs = gw.session.get.call_args[1]
        self.assertGreaterEqual(call_kwargs["params"]["count"], 1)

    def test_503_raises_marginalia_error(self):
        gw, _ = self._gw_with_response(status=503)
        with self.assertRaises(MarginaliaError):
            gw.search("test")

    def test_error_message_does_not_leak_api_key(self):
        gw, _ = self._gw_with_response(status=403)
        with self.assertRaises(MarginaliaError):
            try:
                gw.search("test")
            except MarginaliaError as exc:
                self.assertNotIn("test-key", str(exc))
                raise

    def test_successful_search_returns_list(self):
        body = {"results": [{"title": "T", "url": "http://x.com", "description": "d"}]}
        gw, _ = self._gw_with_response(body=body)
        result = gw.search("test")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
