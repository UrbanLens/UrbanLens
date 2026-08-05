"""Tests for services.apis.locations.routing_resolution - the REData-vs-direct-OSRM choice."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import routing_resolution
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

_MODULE = "urbanlens.dashboard.services.apis.locations.routing_resolution"


class GetRouteBetweenTests(SimpleTestCase):
    def test_redata_configured_uses_redata(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataRoutingGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_route.return_value = {"distance_meters": 100.0, "duration_seconds": 10.0}
            result = routing_resolution.get_route_between((1.0, 2.0), (3.0, 4.0))

        gateway_cls.return_value.get_route.assert_called_once_with([(1.0, 2.0), (3.0, 4.0)], capability="as_given", profile="driving")
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
