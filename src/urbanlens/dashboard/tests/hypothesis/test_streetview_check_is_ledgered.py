"""The map's right-click Street View probe is an ordinary gateway call (P135).

It used to reach Google with a bare ``urlopen``, so it wrote no ``ApiCallLog`` row, sat under no
rate limit and ignored ``UL_ALLOW_OUTBOUND_APIS``. The wire is ``requests.Session.request``, which
the rate-limited session's inner session resolves at call time; ``urlopen`` is patched too so a
regression to it cannot reach the network from the suite.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

SERVICE = "google_street_view_metadata"


def _google_says(status: str) -> mock.Mock:
    response = mock.Mock(ok=True, status_code=200)
    response.json.return_value = {"status": status}
    return response


class _StreetViewCheckCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "test-key")
        )
        self.urlopen = stack.enter_context(
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("bare urlopen"))
        )
        self.wire = stack.enter_context(mock.patch("requests.Session.request", return_value=_google_says("OK")))

    def check(self, lat: float = 40.7128, lng: float = -74.006) -> dict:
        response = self.client.get(reverse("map.streetview_check"), {"lat": str(lat), "lng": str(lng)})
        self.assertEqual(response.status_code, 200)
        return response.json()


class EveryCheckIsInTheLedgerTests(_StreetViewCheckCase):
    def test_a_check_writes_one_api_call_log_row_at_no_cost(self) -> None:
        self.assertEqual(self.check(), {"available": True})

        entry = ApiCallLog.objects.get(service=SERVICE)
        self.assertTrue(entry.success)
        self.assertEqual(entry.cost_estimate, Decimal(0))
        self.urlopen.assert_not_called()

    def test_the_key_never_reaches_the_ledger(self) -> None:
        self.check()

        self.assertNotIn("test-key", ApiCallLog.objects.get(service=SERVICE).endpoint)

    def test_the_service_is_registered_as_free(self) -> None:
        """Google: "Street View Static API metadata requests are available at no charge."""
        defaults = rate_limiter.all_service_defaults()[SERVICE]
        self.assertFalse(defaults.billable)
        self.assertEqual(defaults.cost_per_call, Decimal(0))
        self.assertIsNotNone(defaults.calls_per_minute)


class TheRateLimitAppliesTests(_StreetViewCheckCase):
    def test_a_check_past_the_limit_never_reaches_google(self) -> None:
        rate_limiter.get_limit_config(SERVICE)
        ApiRateLimit.objects.filter(service=SERVICE).update(calls_per_minute=1, min_interval_seconds=None)

        self.assertEqual(self.check(40.0, -74.0), {"available": True})
        refused = self.check(41.0, -75.0)

        self.assertEqual(self.wire.call_count, 1)
        self.assertEqual(refused, {"available": False, "reason": "refused"})
        self.assertTrue(ApiCallLog.objects.filter(service=SERVICE, was_rate_limited=True).exists())


class ARepeatedCheckIsCachedTests(_StreetViewCheckCase):
    def test_a_right_click_storm_on_one_spot_calls_google_once(self) -> None:
        for _ in range(5):
            self.assertEqual(self.check(40.71281, -74.00601), {"available": True})
        self.check(40.71279, -74.00599)

        self.assertEqual(self.wire.call_count, 1)

    def test_a_different_spot_is_its_own_question(self) -> None:
        self.check(40.7128, -74.006)
        self.check(40.8, -74.006)

        self.assertEqual(self.wire.call_count, 2)

    def test_a_failure_is_not_cached(self) -> None:
        self.wire.return_value = _google_says("REQUEST_DENIED")
        self.check()
        self.wire.return_value = _google_says("OK")

        self.assertEqual(self.check(), {"available": True})
        self.assertEqual(self.wire.call_count, 2)


class AFailureIsNotReportedAsNoImageryTests(_StreetViewCheckCase):
    def test_no_imagery_is_a_plain_no(self) -> None:
        self.wire.return_value = _google_says("ZERO_RESULTS")

        self.assertEqual(self.check(), {"available": False})

    def test_a_denied_key_says_so(self) -> None:
        self.wire.return_value = _google_says("REQUEST_DENIED")

        self.assertEqual(self.check(), {"available": False, "reason": "error"})

    def test_a_network_failure_logs_neither_the_key_nor_the_point(self) -> None:
        leaky = "Max retries exceeded with url: /maps/api/streetview/metadata?location=40.7128%2C-74.006&key=test-key"
        self.wire.side_effect = requests.ConnectionError(leaky)

        with self.assertLogs("urbanlens.dashboard.controllers.maps", "WARNING") as logs:
            self.assertEqual(self.check(), {"available": False, "reason": "error"})

        output = "\n".join(logs.output)
        self.assertIn("ConnectionError", output)
        self.assertNotIn("test-key", output)
        self.assertNotIn("40.71", output)

    def test_a_non_finite_coordinate_is_rejected_before_any_call(self) -> None:
        response = self.client.get(reverse("map.streetview_check"), {"lat": "nan", "lng": "0"})

        self.assertEqual(response.status_code, 400)
        self.wire.assert_not_called()


@contextlib.contextmanager
def _development(*, allow: bool | None) -> Iterator[None]:
    with (
        override_settings(ENVIRONMENT_NAME=str(EnvironmentTypes.DEVELOPMENT), TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.allow_outbound_apis", allow),
    ):
        yield


class TheOutboundSwitchAppliesTests(_StreetViewCheckCase):
    def test_a_development_box_does_not_call_google(self) -> None:
        with _development(allow=None):
            self.assertEqual(self.check(), {"available": False, "reason": "refused"})

        self.wire.assert_not_called()
        self.urlopen.assert_not_called()

    def test_allow_outbound_apis_lets_it_through(self) -> None:
        with _development(allow=True):
            self.assertEqual(self.check(), {"available": True})

        self.assertEqual(self.wire.call_count, 1)
