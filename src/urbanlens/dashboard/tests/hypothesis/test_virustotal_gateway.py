"""Tests for VirusTotalGateway - the raw HTTP transport, no verdict policy.

Verdict interpretation (clean/malicious/no-verdict) lives in
virustotal_scan.py and is tested there; this file only covers the HTTP
request/response mechanics, mocking session.get directly (no real network).
"""

from __future__ import annotations

from unittest.mock import Mock

from urbanlens.dashboard.services.apis.security.virustotal import VirusTotalGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError

_SHA256 = "a" * 64


def _gateway(*, api_key: str = "test-key") -> VirusTotalGateway:
    """A gateway with a mocked session, bypassing the real rate-limited one.

    Gateway.__post_init__ only swaps in the DB-writing _RateLimitedSession
    when `session` is still the default plain requests.Session instance -
    passing a Mock() here skips that entirely.
    """
    return VirusTotalGateway(session=Mock(), api_key=api_key)


def _response(status_code: int, json_body: object = None, *, json_raises: bool = False) -> Mock:
    response = Mock()
    response.status_code = status_code
    if json_raises:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_body
    return response


def test_known_hash_returns_the_attributes_dict() -> None:
    gateway = _gateway()
    attributes = {"last_analysis_stats": {"malicious": 0, "harmless": 70}}
    gateway.session.get.return_value = _response(200, {"data": {"attributes": attributes}})

    assert gateway.get_file_report(_SHA256) == attributes  # nosec B101


def test_unknown_hash_returns_none() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(404)

    assert gateway.get_file_report(_SHA256) is None  # nosec B101


def test_unauthorized_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(401)

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for a 401")


def test_rate_limited_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(429)

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for a 429")


def test_server_error_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(500)

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for a 500")


def test_unparseable_json_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(200, json_raises=True)

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for an unparseable body")


def test_missing_data_key_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(200, {"nope": "wrong shape"})

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for a body missing data.attributes")


def test_non_dict_attributes_raises() -> None:
    gateway = _gateway()
    gateway.session.get.return_value = _response(200, {"data": {"attributes": ["not", "a", "dict"]}})

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for non-object attributes")


def test_transport_error_raises_gateway_request_error() -> None:
    gateway = _gateway()
    gateway.session.get.side_effect = OSError("connection refused")

    try:
        gateway.get_file_report(_SHA256)
    except GatewayRequestError:
        return
    raise AssertionError("expected GatewayRequestError for a transport failure")


def test_post_init_attaches_the_api_key_header() -> None:
    gateway = _gateway(api_key="abc123")

    gateway.session.headers.update.assert_called_with({"x-apikey": "abc123", "Accept": "application/json"})
