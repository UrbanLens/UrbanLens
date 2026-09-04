"""Tests for services.apis.locations.routing_resolution - the REData-vs-direct-OSRM choice."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import routing_resolution
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.routing import osrm
from urbanlens.UrbanLens.settings.app import settings as app_settings

_MODULE = "urbanlens.dashboard.services.apis.locations.routing_resolution"


class GetRouteBetweenTests(SimpleTestCase):
    def test_redata_configured_uses_redata(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataRoutingGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_route.return_value = {"distance_meters": 100.0, "duration_seconds": 10.0}
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        gateway_cls.return_value.get_route.assert_called_once_with(
            [(1.0, 2.0), (3.0, 4.0)], capability="as_given", profile="driving"
        )
        self.assertEqual(result, {"distance_meters": 100.0, "duration_seconds": 10.0})

    def test_redata_not_configured_uses_osrm(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch("urbanlens.dashboard.services.apis.routing.osrm.OSRMGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_route_between.return_value = {"distance_meters": 50.0, "duration_seconds": 5.0}
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        gateway_cls.return_value.get_route_between.assert_called_once_with((1.0, 2.0), (3.0, 4.0))
        self.assertEqual(result, {"distance_meters": 50.0, "duration_seconds": 5.0})

    def test_redata_failure_falls_back_to_osrm(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataRoutingGateway") as gateway_cls,
            mock.patch("urbanlens.dashboard.services.apis.routing.osrm.OSRMGateway") as osrm_cls,
        ):
            gateway_cls.return_value.get_route.side_effect = LocationContextUnavailableError("source_error", "boom")
            osrm_cls.return_value.get_route_between.return_value = None
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        osrm_cls.return_value.get_route_between.assert_called_once()
        self.assertIsNone(result)


class OsrmBaseUrlTests(SimpleTestCase):
    """``UL_OSRM_BASE_URL``, and why the default has to be re-read per instance.

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

    def test_the_demo_host_is_reachable_through_the_egress_filter(self) -> None:
        # The assistant's routing tool runs in ai-worker, behind a deny-by-default
        # egress proxy; a base URL nothing allowlists is a tool that always fails.
        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parents[5]
        allowlist = (repo_root / "src/urbanlens/config/egress/filter").read_text()
        self.assertIn("router\\.project-osrm\\.org", allowlist)
