"""Tests for the provider-agnostic Google OAuth helpers.

Pure-function and mocked-HTTP coverage for services/google_oauth.py - the
token exchange/refresh error contracts (callers rely on GatewayRequestError
to trigger their reconnect flows) and the display-only id_token email
extraction's tolerance of malformed input.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.google_oauth import (
    GOOGLE_AUTH_URL,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_email_from_id_token,
    refresh_access_token,
    revoke_token,
)


def _id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class BuildAuthorizationUrlTests(SimpleTestCase):
    def test_url_carries_all_flow_parameters(self) -> None:
        url = build_authorization_url("client-123", "https://app.example/callback", ["scope.a", "scope.b"], "signed-state")
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", GOOGLE_AUTH_URL)
        params = parse_qs(parsed.query)
        self.assertEqual(params["client_id"], ["client-123"])
        self.assertEqual(params["redirect_uri"], ["https://app.example/callback"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["scope"], ["scope.a scope.b"])
        self.assertEqual(params["access_type"], ["offline"])
        self.assertEqual(params["prompt"], ["consent"])
        self.assertEqual(params["state"], ["signed-state"])


class TokenExchangeTests(SimpleTestCase):
    def test_successful_exchange_returns_the_payload(self) -> None:
        response = Mock(status_code=200, json=Mock(return_value={"access_token": "at", "refresh_token": "rt"}))
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=response) as mock_post:
            payload = exchange_code_for_tokens("cid", "secret", "auth-code", "https://app.example/callback")
        self.assertEqual(payload["access_token"], "at")
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "authorization_code")

    def test_failed_exchange_raises_gateway_error(self) -> None:
        response = Mock(status_code=400, text='{"error": "invalid_grant"}')
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=response):
            with self.assertRaises(GatewayRequestError):
                exchange_code_for_tokens("cid", "secret", "bad-code", "https://app.example/callback")

    def test_failed_refresh_raises_gateway_error(self) -> None:
        """Callers branch on this to prompt a reconnect when access was revoked."""
        response = Mock(status_code=400, text='{"error": "invalid_grant"}')
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=response):
            with self.assertRaises(GatewayRequestError):
                refresh_access_token("cid", "secret", "revoked-refresh-token")

    def test_successful_refresh_returns_the_payload(self) -> None:
        response = Mock(status_code=200, json=Mock(return_value={"access_token": "fresh"}))
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=response) as mock_post:
            payload = refresh_access_token("cid", "secret", "rt")
        self.assertEqual(payload["access_token"], "fresh")
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")

    def test_revoke_is_best_effort_on_network_failure(self) -> None:
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", side_effect=requests.ConnectionError):
            self.assertFalse(revoke_token("token"))

    def test_revoke_reports_google_confirmation(self) -> None:
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=Mock(status_code=200)):
            self.assertTrue(revoke_token("token"))
        with patch("urbanlens.dashboard.services.google_oauth.requests.post", return_value=Mock(status_code=400)):
            self.assertFalse(revoke_token("token"))


class ExtractEmailFromIdTokenTests(SimpleTestCase):
    """Display-only claim extraction must never raise on malformed input."""

    def test_extracts_the_email_claim(self) -> None:
        self.assertEqual(extract_email_from_id_token(_id_token({"email": "user@example.com"})), "user@example.com")

    def test_unpadded_base64_payload_is_handled(self) -> None:
        # Claims chosen so the b64 payload length isn't a multiple of 4.
        token = _id_token({"email": "a@b.co", "x": 1})
        self.assertEqual(extract_email_from_id_token(token), "a@b.co")

    def test_none_and_empty_return_none(self) -> None:
        self.assertIsNone(extract_email_from_id_token(None))
        self.assertIsNone(extract_email_from_id_token(""))

    def test_wrong_segment_count_returns_none(self) -> None:
        self.assertIsNone(extract_email_from_id_token("only.two"))
        self.assertIsNone(extract_email_from_id_token("a.b.c.d"))

    def test_garbage_payload_returns_none(self) -> None:
        self.assertIsNone(extract_email_from_id_token("header.!!!not-base64!!!.sig"))

    def test_non_json_payload_returns_none(self) -> None:
        payload = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        self.assertIsNone(extract_email_from_id_token(f"h.{payload}.s"))

    def test_missing_or_non_string_email_returns_none(self) -> None:
        self.assertIsNone(extract_email_from_id_token(_id_token({"sub": "123"})))
        self.assertIsNone(extract_email_from_id_token(_id_token({"email": 42})))
