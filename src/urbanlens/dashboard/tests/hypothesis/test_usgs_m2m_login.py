"""A failed USGS M2M login is reported as the provider failure, not retried as an anonymous request."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import usgs
from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _response(*, json_data=None, status: int = 200, json_error: Exception | None = None) -> mock.MagicMock:
    response = mock.MagicMock()
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError(f"{status}") if status >= 400 else None
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = json_data
    return response


class M2mLoginTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.delete(usgs._M2M_SESSION_CACHE_KEY)
        self.addCleanup(cache.delete, usgs._M2M_SESSION_CACHE_KEY)
        self.session = mock.MagicMock()
        self.gateway = UsgsGateway(api_key="app-token", username="explorer", session=self.session)

    def test_a_failed_login_raises_and_never_sends_the_request_unauthenticated(self) -> None:
        failures = {
            "unreachable": requests.ConnectionError("down"),
            "timeout": requests.Timeout("slow"),
            "rejected": _response(status=401),
            "not json": _response(json_error=ValueError("no json")),
            "no token": _response(json_data={"data": None, "errorCode": "AUTH_INVALID"}),
            "not an object": _response(json_data=["unexpected"]),
        }
        for name, failure in failures.items():
            self.session.reset_mock(return_value=True, side_effect=True)
            if isinstance(failure, Exception):
                self.session.post.side_effect = failure
            else:
                self.session.post.return_value = failure
            with self.subTest(name), self.assertRaises(GatewayRequestError):
                self.gateway.dataset_search(datasetName="landsat")

            self.assertEqual(self.session.post.call_count, 1, "only the login was sent")
            self.assertTrue(self.session.post.call_args.args[0].endswith("/login-token"))

    def test_a_good_login_authenticates_the_request_and_is_cached(self) -> None:
        self.session.post.side_effect = [
            _response(json_data={"data": "session-token"}),
            _response(json_data={"data": []}),
            _response(json_data={"data": []}),
        ]

        self.gateway.dataset_search(datasetName="landsat")
        self.gateway.dataset_search(datasetName="landsat")

        calls = self.session.post.call_args_list
        self.assertEqual(len(calls), 3, "one login, then two requests on the cached session")
        self.assertEqual(calls[1].kwargs["headers"], {"X-Auth-Token": "session-token"})
        self.assertEqual(calls[2].kwargs["headers"], {"X-Auth-Token": "session-token"})

    def test_without_credentials_the_request_goes_as_it_always_did(self) -> None:
        gateway = UsgsGateway(api_key=None, username=None, session=self.session)
        self.session.post.return_value = _response(json_data={"data": []})

        gateway.dataset_search(datasetName="landsat")

        self.session.post.assert_called_once()
        self.assertIsNone(self.session.post.call_args.kwargs["headers"])
