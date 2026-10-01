"""An ``ApiCallLog`` row records the upstream's HTTP status, so a failed call can be diagnosed after the app log rotates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
from unittest import mock

import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.rate_limiter import log_api_call

_SERVICE = "status_code_probe"


@dataclass(slots=True, kw_only=True)
class _ProbeGateway(Gateway):
    service_key: ClassVar[str] = _SERVICE

    def fetch(self) -> requests.Response:
        return self.session.get("https://tiles.example.com/1/2/3")


def _response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = b""
    return response


class GatewayCallRecordsStatusTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("check_rate_limit", "service_is_enabled"):
            patcher = mock.patch(f"urbanlens.dashboard.services.core.rate_limiter.{name}", return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, status: int) -> ApiCallLog:
        with mock.patch.object(requests.Session, "request", return_value=_response(status)):
            _ProbeGateway().fetch()
        return ApiCallLog.objects.get(service=_SERVICE)

    def test_a_503_is_recorded(self) -> None:
        entry = self._call(503)
        self.assertFalse(entry.success)
        self.assertEqual(entry.status_code, 503)

    def test_a_success_is_recorded_as_200(self) -> None:
        entry = self._call(200)
        self.assertTrue(entry.success)
        self.assertEqual(entry.status_code, 200)

    def test_a_call_that_never_got_a_response_has_no_status(self) -> None:
        with (
            mock.patch.object(requests.Session, "request", side_effect=requests.ConnectionError("refused")),
            self.assertRaises(requests.ConnectionError),
        ):
            _ProbeGateway().fetch()
        entry = ApiCallLog.objects.get(service=_SERVICE)
        self.assertFalse(entry.success)
        self.assertIsNone(entry.status_code)


class LogApiCallStatusTests(TestCase):
    def test_a_manual_caller_can_record_the_status(self) -> None:
        log_api_call(_SERVICE, success=False, status_code=429)
        self.assertEqual(ApiCallLog.objects.get(service=_SERVICE).status_code, 429)
