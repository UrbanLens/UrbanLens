"""Tests for BraveSearchGateway.

Covers redact_secret, _validate, _parse, and search().
All HTTP is mocked - no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings as hyp_settings, strategies as st
import pytest
from requests import HTTPError

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchError, BraveSearchGateway
from urbanlens.dashboard.services.redact import redact_secret

_hyp = hyp_settings(max_examples=50, deadline=None)


# ---------------------------------------------------------------------------
# redact_secret
# ---------------------------------------------------------------------------

class MaskSecretTests(TestCase):
    """redact_secret fingerprints API keys without ever revealing them."""

    def test_none_returns_missing(self):
        self.assertEqual(redact_secret(None), "<missing>")

    def test_empty_string_returns_missing(self):
        self.assertEqual(redact_secret(""), "<missing>")

    def test_key_never_reveals_any_substring(self):
        key = "ABCD_MIDDLE_1234"
        result = redact_secret(key)
        self.assertNotIn("ABCD", result)
        self.assertNotIn("1234", result)
        self.assertNotIn("_MIDDLE_", result)

    def test_same_key_produces_same_fingerprint(self):
        self.assertEqual(redact_secret("some-key"), redact_secret("some-key"))

    def test_different_keys_produce_different_fingerprints(self):
        self.assertNotEqual(redact_secret("some-key"), redact_secret("other-key"))

    @given(st.text(min_size=1, max_size=64, alphabet=st.characters(whitelist_categories=("L", "N"))))
    @_hyp
    def test_key_never_equals_original(self, key: str):
        self.assertNotEqual(redact_secret(key), key)

    @given(st.text(min_size=1, max_size=64, alphabet=st.characters(whitelist_categories=("L", "N"))))
    @_hyp
    def test_key_never_appears_as_substring_of_result(self, key: str):
        self.assertNotIn(key, redact_secret(key))


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------

class BraveValidateTests(TestCase):
    """_validate raises BraveSearchError when the API key is missing."""

    def _gw(self, key: str | None) -> BraveSearchGateway:
        gw = object.__new__(BraveSearchGateway)
        object.__setattr__(gw, "api_key", key)
        object.__setattr__(gw, "base_url", "https://example.com")
        object.__setattr__(gw, "session", MagicMock())
        return gw

    def test_empty_key_raises(self):
        with self.assertRaises(BraveSearchError, msg="Should raise for empty key"):
            self._gw("").search("test")

    def test_none_key_raises(self):
        with self.assertRaises(BraveSearchError):
            self._gw(None).search("test")

    def test_valid_key_does_not_raise_on_validate(self):
        gw = self._gw("somekey")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {}
        gw.session.get.return_value = mock_resp
        # No BraveSearchError raised for key validation
        gw.search("test")

    def test_error_message_references_env_var(self):
        gw = self._gw("")
        gw2 = object.__new__(BraveSearchGateway)
        object.__setattr__(gw2, "api_key", "")
        object.__setattr__(gw2, "base_url", "https://x.com")
        object.__setattr__(gw2, "session", MagicMock())
        with self.assertRaises(BraveSearchError) as ctx:
            gw2._validate()
        self.assertIn("UL_BRAVE_SEARCH_API_KEY", str(ctx.exception))


# ---------------------------------------------------------------------------
# _parse
# ---------------------------------------------------------------------------

def _make_gw() -> BraveSearchGateway:
    gw = object.__new__(BraveSearchGateway)
    object.__setattr__(gw, "api_key", "test-key")
    object.__setattr__(gw, "base_url", "https://example.com")
    object.__setattr__(gw, "session", MagicMock())
    return gw


class BraveParseTests(TestCase):
    """_parse converts the Brave JSON structure to normalised dicts."""

    def setUp(self):
        self.gw = _make_gw()

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(self.gw._parse({}), [])

    def test_no_web_key_returns_empty_list(self):
        self.assertEqual(self.gw._parse({"other": {}}), [])

    def test_single_result_extracts_title(self):
        data = {"web": {"results": [{"title": "Abandoned Mill", "url": "http://x.com", "description": "desc"}]}}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["title"], "Abandoned Mill")

    def test_single_result_maps_url_to_link(self):
        data = {"web": {"results": [{"title": "T", "url": "http://example.com/page", "description": "d"}]}}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["link"], "http://example.com/page")

    def test_single_result_maps_description_to_snippet(self):
        data = {"web": {"results": [{"title": "T", "url": "http://x.com", "description": "A detailed snippet"}]}}
        result = self.gw._parse(data)
        self.assertEqual(result[0]["snippet"], "A detailed snippet")

    def test_multiple_results_all_returned(self):
        items = [{"title": f"T{i}", "url": f"http://x.com/{i}", "description": "d"} for i in range(5)]
        data = {"web": {"results": items}}
        result = self.gw._parse(data)
        self.assertEqual(len(result), 5)

    def test_missing_fields_default_to_none(self):
        data = {"web": {"results": [{}]}}
        result = self.gw._parse(data)
        self.assertIsNone(result[0]["title"])
        self.assertIsNone(result[0]["link"])
        self.assertIsNone(result[0]["snippet"])

    def test_each_result_has_title_link_snippet_keys(self):
        data = {"web": {"results": [{"title": "T", "url": "http://x.com", "description": "d"}]}}
        result = self.gw._parse(data)
        self.assertIn("title", result[0])
        self.assertIn("link", result[0])
        self.assertIn("snippet", result[0])

    @given(st.lists(st.text(min_size=1, max_size=40), min_size=0, max_size=20))
    @_hyp
    def test_parse_count_matches_results_count(self, titles: list[str]):
        items = [{"title": t, "url": "http://x.com", "description": "d"} for t in titles]
        data = {"web": {"results": items}}
        result = self.gw._parse(data)
        self.assertEqual(len(result), len(titles))


# ---------------------------------------------------------------------------
# search() - HTTP integration
# ---------------------------------------------------------------------------

class BraveSearchHTTPTests(TestCase):
    """search() sends the correct request and handles HTTP errors."""

    def _gw_with_response(self, status: int = 200, body: dict | None = None) -> tuple[BraveSearchGateway, MagicMock]:
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

    def test_passes_max_results_as_count(self):
        gw, _ = self._gw_with_response()
        gw.search("test", max_results=5)
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["params"]["count"], 5)

    def test_count_clamped_to_20(self):
        gw, _ = self._gw_with_response()
        gw.search("test", max_results=50)
        call_kwargs = gw.session.get.call_args[1]
        self.assertLessEqual(call_kwargs["params"]["count"], 20)

    def test_count_clamped_to_minimum_1(self):
        gw, _ = self._gw_with_response()
        gw.search("test", max_results=0)
        call_kwargs = gw.session.get.call_args[1]
        self.assertGreaterEqual(call_kwargs["params"]["count"], 1)

    def test_sends_subscription_token_header(self):
        gw, _ = self._gw_with_response()
        gw.search("test")
        call_kwargs = gw.session.get.call_args[1]
        self.assertEqual(call_kwargs["headers"]["X-Subscription-Token"], "test-key")

    def test_403_raises_brave_search_error(self):
        gw, _ = self._gw_with_response(status=403)
        with self.assertRaises(BraveSearchError):
            gw.search("test")

    def test_500_raises_brave_search_error(self):
        gw, _ = self._gw_with_response(status=500)
        with self.assertRaises(BraveSearchError):
            gw.search("test")

    def test_error_message_does_not_leak_api_key(self):
        gw, _ = self._gw_with_response(status=403)
        with self.assertRaises(BraveSearchError):
            try:
                gw.search("test")
            except BraveSearchError as exc:
                self.assertNotIn("test-key", str(exc))
                raise

    def test_successful_search_returns_list(self):
        body = {"web": {"results": [{"title": "T", "url": "http://x.com", "description": "d"}]}}
        gw, _ = self._gw_with_response(body=body)
        result = gw.search("test")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_raise_for_status_is_called(self):
        gw, mock_resp = self._gw_with_response()
        gw.search("test")
        mock_resp.raise_for_status.assert_called_once()
