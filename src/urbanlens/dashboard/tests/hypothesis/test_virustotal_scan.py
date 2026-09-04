"""Tests for virustotal_scan.verdict_for_checksum - the VirusTotal verdict policy.

Pure function tests against a mocked VirusTotalGateway; no real network, no
DB. settings/test.py forces AppSettings.virustotal_api_key=None for the whole
suite, so each test that needs the "configured" path patches it back.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, ServiceDisabledError
from urbanlens.dashboard.services.security import virustotal_scan

_SHA256 = "a" * 64


def test_no_api_key_raises_without_constructing_the_gateway() -> None:
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new=None),
        patch.object(virustotal_scan, "VirusTotalGateway") as gateway_cls,
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            pass
        else:
            raise AssertionError("expected VirusTotalNoVerdictError")
    gateway_cls.assert_not_called()


def test_unknown_hash_raises() -> None:
    gateway = Mock()
    gateway.get_file_report.return_value = None
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("expected VirusTotalNoVerdictError")


def test_explicit_clean_verdict_returns_none() -> None:
    gateway = Mock()
    gateway.get_file_report.return_value = {
        "last_analysis_stats": {"malicious": 0, "suspicious": 0, "undetected": 3, "harmless": 67}
    }
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        assert virustotal_scan.verdict_for_checksum(_SHA256) is None  # nosec B101


def test_malicious_verdict_returns_a_rejection_message() -> None:
    gateway = Mock()
    gateway.get_file_report.return_value = {"last_analysis_stats": {"malicious": 2, "suspicious": 0, "undetected": 68}}
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        error = virustotal_scan.verdict_for_checksum(_SHA256)
    assert error is not None  # nosec B101
    assert "VirusTotal" in error  # nosec B101


def test_suspicious_only_is_not_treated_as_clean() -> None:
    """malicious == 0 alone must not read as safe - a suspicious-only verdict
    is exactly the ambiguous case that must not fall through as clean."""
    gateway = Mock()
    gateway.get_file_report.return_value = {"last_analysis_stats": {"malicious": 0, "suspicious": 1, "undetected": 69}}
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        error = virustotal_scan.verdict_for_checksum(_SHA256)
    assert error is not None  # nosec B101


def test_all_zero_stats_raises_rather_than_reading_as_clean() -> None:
    """The core correctness guard: absence of a positive finding must never be
    conflated with an explicit clean verdict when no engine actually reported."""
    gateway = Mock()
    gateway.get_file_report.return_value = {
        "last_analysis_stats": {"malicious": 0, "suspicious": 0, "undetected": 0, "harmless": 0}
    }
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("an all-zero analysis was treated as an explicit clean verdict")


def test_missing_stats_key_raises_rather_than_reading_as_clean() -> None:
    gateway = Mock()
    gateway.get_file_report.return_value = {}
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("a missing last_analysis_stats was treated as an explicit clean verdict")


def test_gateway_request_error_raises_no_verdict() -> None:
    gateway = Mock()
    gateway.get_file_report.side_effect = GatewayRequestError("boom")
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("expected VirusTotalNoVerdictError")


def test_rate_limit_exceeded_raises_no_verdict() -> None:
    """Quota exhaustion (calls_per_day/calls_per_minute) must fail over silently, not
    raise something the caller doesn't already handle."""
    gateway = Mock()
    gateway.get_file_report.side_effect = RateLimitExceededError("virustotal")
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("expected VirusTotalNoVerdictError")


def test_service_disabled_raises_no_verdict() -> None:
    gateway = Mock()
    gateway.get_file_report.side_effect = ServiceDisabledError("virustotal")
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("expected VirusTotalNoVerdictError")


def test_transport_error_raises_no_verdict() -> None:
    gateway = Mock()
    gateway.get_file_report.side_effect = OSError("connection refused")
    with (
        patch.object(virustotal_scan.app_settings, "virustotal_api_key", new="test-key"),
        patch.object(virustotal_scan, "VirusTotalGateway", return_value=gateway),
    ):
        try:
            virustotal_scan.verdict_for_checksum(_SHA256)
        except virustotal_scan.VirusTotalNoVerdictError:
            return
    raise AssertionError("expected VirusTotalNoVerdictError")
