"""A throttled REData answers the media proxies' callers "retry later", not "gone".

Reproduces the HRSH location run's wiki gallery failure (2026-09-23 20:10): REData's per-key lookup budget
answered every CRIS attachment download 429 ("Expected available in 989 seconds"), the gateway raised the same
PropertyRecordsUnavailableError it raises for a missing attachment, and the proxy turned that into 404. The
gallery reads a 404 as "no preview is coming", and the page guard reports it as a failure.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import Client
from django.urls import reverse
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsBusyError,
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_MAX_SECONDS, UpstreamBusyError

_THROTTLED = {"error": "throttled", "message": "Request was throttled. Expected available in 989 seconds."}


def _response(status_code: int, body: dict, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, headers=headers or {})
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _redata(session: mock.Mock) -> RedataGateway:
    return RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class RedataDownloadsReportThrottlingTests(SimpleTestCase):
    DOWNLOADS = (
        ("download_listing_photo", ("listing-1", 1)),
        ("download_cultural_resource_attachment", ("res-1", 5)),
        ("download_extracted_image", ("res-1", 5, 7)),
    )

    def test_a_429_is_busy_with_the_wait_redata_named(self) -> None:
        for method, args in self.DOWNLOADS:
            with self.subTest(method=method):
                session = mock.Mock()
                session.get.return_value = _response(429, _THROTTLED, {"Retry-After": "120"})
                with pytest.raises(PropertyRecordsBusyError) as caught:
                    getattr(_redata(session), method)(*args)
                self.assertEqual(caught.value.retry_after, 120)

    def test_the_wait_is_read_from_the_body_when_there_is_no_header(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(429, {"message": "Expected available in 45 seconds."})
        with pytest.raises(PropertyRecordsBusyError) as caught:
            _redata(session).download_cultural_resource_attachment("res-1", 5)
        self.assertEqual(caught.value.retry_after, 45)

    def test_a_long_wait_is_capped(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(429, _THROTTLED)
        with pytest.raises(PropertyRecordsBusyError) as caught:
            _redata(session).download_cultural_resource_attachment("res-1", 5)
        self.assertEqual(caught.value.retry_after, UPSTREAM_BUSY_MAX_SECONDS)

    def test_a_503_from_the_source_is_busy_too(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "cris_unavailable", "message": "CRIS is down"})
        with pytest.raises(PropertyRecordsBusyError):
            _redata(session).download_cultural_resource_attachment("res-1", 5)

    def test_a_404_is_still_gone_not_busy(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(404, {"error": "attachment_unavailable", "message": "gone"})
        with pytest.raises(PropertyRecordsUnavailableError) as caught:
            _redata(session).download_cultural_resource_attachment("res-1", 5)
        self.assertNotIsInstance(caught.value, UpstreamBusyError)

    def test_the_cid_gateway_reports_throttling_the_same_way(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(429, _THROTTLED, {"Retry-After": "30"})
        gateway = RedataCidGateway(base_url="https://redata.example.test", api_key="test-key", session=session)
        with pytest.raises(UpstreamBusyError) as caught:
            gateway.download_media(123, 1)
        self.assertEqual(caught.value.retry_after, 30)


class TheProxyAsksForARetryTests(SimpleTestCase):
    def _get(
        self,
        url: str,
        error: Exception,
        gateway: type = RedataGateway,
        method: str = "download_cultural_resource_attachment",
        **params: str,
    ):
        with (
            mock.patch.object(gateway, "__post_init__", return_value=None),
            mock.patch.object(gateway, method, side_effect=error),
        ):
            return Client().get(url, params)

    def assert_retry_later(self, response, seconds: int) -> None:
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], str(seconds))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_a_throttled_preview_is_503_with_retry_after(self) -> None:
        url = reverse("pin.cris.attachment", args=["res-1", 5])
        busy = PropertyRecordsBusyError("rate_limited", "throttled", retry_after=120)
        self.assert_retry_later(self._get(url, busy, preview="1"), 120)

    def test_a_throttled_original_is_503_with_retry_after(self) -> None:
        url = reverse("pin.cris.attachment", args=["res-1", 5])
        busy = PropertyRecordsBusyError("rate_limited", "throttled", retry_after=60)
        self.assert_retry_later(self._get(url, busy), 60)

    def test_a_throttled_cid_media_item_is_503(self) -> None:
        url = reverse("pin.place_cid.media", args=[123456789012345678, 1])
        busy = UpstreamBusyError("throttled", retry_after=30)
        self.assert_retry_later(self._get(url, busy, gateway=RedataCidGateway, method="download_media"), 30)

    def test_a_missing_attachment_is_still_404(self) -> None:
        url = reverse("pin.cris.attachment", args=["res-1", 5])
        gone = PropertyRecordsUnavailableError("attachment_unavailable", "gone")
        self.assertEqual(self._get(url, gone, preview="1").status_code, 404)
