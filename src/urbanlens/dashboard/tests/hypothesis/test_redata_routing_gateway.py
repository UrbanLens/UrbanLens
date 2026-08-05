"""Tests for RedataRoutingGateway against REData's shipped contract
(``../REData/docs/api-reference.md``, "POST /routes/ - route between waypoints").
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_routing_gateway import RedataRoutingGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataRoutingGateway:
    return RedataRoutingGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetRouteTests(SimpleTestCase):
    def test_returns_distance_and_duration(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"route": {"distance_meters": 18000.0, "duration_seconds": 1200.0}, "waypoint_order": [0, 1], "available_capabilities": ["as_given"]})

        result = _gateway(session).get_route([(41.0, -73.9), (41.1, -73.8)])

        self.assertEqual(result, {"distance_meters": 18000.0, "duration_seconds": 1200.0})
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["waypoints"], [[41.0, -73.9], [41.1, -73.8]])
        self.assertEqual(body["capability"], "as_given")
        self.assertEqual(body["profile"], "driving")

    def test_null_route_returns_none(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"route": None, "waypoint_order": [0, 1], "available_capabilities": ["as_given"]})

        self.assertIsNone(_gateway(session).get_route([(41.0, -73.9), (41.1, -73.8)]))

    def test_unconfigured_capability_raises(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(503, {"error": "capability_unavailable", "message": "optimized routing not configured"})

        with self.assertRaises(LocationContextUnavailableError):
            _gateway(session).get_route([(41.0, -73.9), (41.1, -73.8)], capability="optimized")
