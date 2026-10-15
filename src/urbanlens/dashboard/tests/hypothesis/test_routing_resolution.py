"""Tests for services.apis.locations.routing_resolution - the choice among route providers."""

from __future__ import annotations

from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import ROUTE_PROVIDERS
from urbanlens.dashboard.services.apis.locations import routing_resolution
from urbanlens.dashboard.services.apis.locations.routing_resolution import OsrmRouteProvider, RouteProvider
from urbanlens.dashboard.services.apis.routing import osrm
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.UrbanLens.settings.app import settings as app_settings


class _FakeRoutes(RouteProvider):
    """Answers with ``answer``, or raises ``failure``; remembers what it was asked."""

    def __init__(
        self,
        key: str,
        answer: dict[str, Any] | None = None,
        *,
        failure: Exception | None = None,
        is_available: bool = True,
    ) -> None:
        self.key = key
        self.answer = answer
        self.failure = failure
        self.is_available = is_available
        self.asked: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def available(self) -> bool:
        return self.is_available

    def get_route(self, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        self.asked.append((origin, destination))
        if self.failure is not None:
            raise self.failure
        return self.answer


class GetRouteBetweenTests(SimpleTestCase):
    def test_the_first_available_provider_answers(self) -> None:
        skipped = _FakeRoutes("skipped", {"distance_meters": 1.0, "duration_seconds": 1.0}, is_available=False)
        first = _FakeRoutes("first", {"distance_meters": 100.0, "duration_seconds": 10.0})
        second = _FakeRoutes("second", {"distance_meters": 50.0, "duration_seconds": 5.0})
        with ROUTE_PROVIDERS.overridden([skipped, first, second]):
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        self.assertEqual(result, {"distance_meters": 100.0, "duration_seconds": 10.0})
        self.assertEqual(first.asked, [((1.0, 2.0), (3.0, 4.0))])
        self.assertEqual((skipped.asked, second.asked), ([], []))

    def test_no_route_is_the_answer_and_the_next_provider_is_not_asked(self) -> None:
        first = _FakeRoutes("first")
        second = _FakeRoutes("second", {"distance_meters": 50.0, "duration_seconds": 5.0})
        with ROUTE_PROVIDERS.overridden([first, second]):
            self.assertIsNone(routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0)))

        self.assertEqual(second.asked, [])

    def test_an_unavailable_provider_hands_over_to_the_next(self) -> None:
        failing = _FakeRoutes("failing", failure=ProviderUnavailableError("boom"))
        fallback = _FakeRoutes("fallback", {"distance_meters": 50.0, "duration_seconds": 5.0})
        with ROUTE_PROVIDERS.overridden([failing, fallback]):
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        self.assertEqual(result, {"distance_meters": 50.0, "duration_seconds": 5.0})

    def test_every_provider_failing_is_no_route(self) -> None:
        with ROUTE_PROVIDERS.overridden([_FakeRoutes("a", failure=ProviderUnavailableError("boom"))]):
            self.assertIsNone(routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0)))

    def test_no_provider_is_no_route(self) -> None:
        with ROUTE_PROVIDERS.overridden([]):
            self.assertIsNone(routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0)))

    def test_osrm_provider_asks_the_osrm_gateway(self) -> None:
        with (
            ROUTE_PROVIDERS.overridden([OsrmRouteProvider()]),
            mock.patch("urbanlens.dashboard.services.apis.routing.osrm.OSRMGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_route_between.return_value = {"distance_meters": 50.0, "duration_seconds": 5.0}
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        gateway_cls.return_value.get_route_between.assert_called_once_with((1.0, 2.0), (3.0, 4.0))
        self.assertEqual(result, {"distance_meters": 50.0, "duration_seconds": 5.0})


class OsrmBaseUrlTests(SimpleTestCase):
    """
    ``UL_OSRM_BASE_URL``, and why the default has to be re-read per instance.

    Without a configurable base URL every deployment routes through OSRM's
    public demo server, which that project documents as dev/testing only.
    """

    def test_the_setting_exists(self) -> None:
        # A gateway reading a field the settings model never declared would
        # silently keep the demo default forever.
        self.assertIn("osrm_base_url", type(app_settings).model_fields)

    def test_unset_falls_back_to_the_demo_server(self) -> None:
        with mock.patch.object(app_settings, "osrm_base_url", None):
            self.assertEqual(osrm.OSRMGateway().base_url, osrm._DEMO_BASE_URL)

    def test_a_configured_url_is_used(self) -> None:
        with mock.patch.object(app_settings, "osrm_base_url", "http://osrm.internal:5000"):
            self.assertEqual(osrm.OSRMGateway().base_url, "http://osrm.internal:5000")

    def test_the_setting_is_read_per_instance_not_at_import(self) -> None:
        # The bug a bare dataclass default would reintroduce: the default
        # expression is evaluated once at class-definition time, so an operator
        # changing the setting (or a test patching it) never reaches a gateway
        # built later in the same process.
        with mock.patch.object(app_settings, "osrm_base_url", "http://first:5000"):
            first = osrm.OSRMGateway().base_url
        with mock.patch.object(app_settings, "osrm_base_url", "http://second:5000"):
            second = osrm.OSRMGateway().base_url
        self.assertEqual((first, second), ("http://first:5000", "http://second:5000"))

    def test_an_explicit_argument_still_wins(self) -> None:
        with mock.patch.object(app_settings, "osrm_base_url", "http://configured:5000"):
            self.assertEqual(osrm.OSRMGateway(base_url="http://explicit:5000").base_url, "http://explicit:5000")
