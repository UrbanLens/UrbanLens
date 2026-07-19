"""Tests for the NPS gateways' boundary-containment lookup.

The pin-detail NPS panel shows a national park only when the pin's coordinates
fall *inside* that park's boundary (not merely near it). These tests cover the
server-side point-in-polygon lookup (`NPSMapGateway.check_coordinates_within_park`)
and the containment-driven detail fetch (`NPSGateway.find_park_containing_location`).

All HTTP calls are mocked so no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.parks.nps.map import NPSMapGateway
from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

# Coordinates inside Yellowstone (US) and in Paris (non-US) for the geo guard.
_YELLOWSTONE = (44.6, -110.5)
_PARIS = (48.8566, 2.3522)


def _json_response(payload: dict) -> MagicMock:
    """Build a mock requests.Response whose .json() returns *payload*."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _map_gateway() -> NPSMapGateway:
    """Return an NPSMapGateway with a stub session (no real HTTP)."""
    return NPSMapGateway(session=MagicMock())


def _parks_gateway() -> NPSGateway:
    """Return an NPSGateway with a fake key and stub session (no real HTTP)."""
    return NPSGateway(api_key="test-key", session=MagicMock())


class CheckCoordinatesWithinParkTests(SimpleTestCase):
    """check_coordinates_within_park returns the containing unit's lower-cased code."""

    def test_returns_lowercased_unit_code_for_containing_park(self):
        gw = _map_gateway()
        gw.session.get.return_value = _json_response({"features": [{"attributes": {"UNIT_CODE": "YELL", "UNIT_NAME": "Yellowstone"}}]})

        self.assertEqual(gw.check_coordinates_within_park(*_YELLOWSTONE), "yell")

    def test_returns_none_when_point_is_outside_every_boundary(self):
        gw = _map_gateway()
        gw.session.get.return_value = _json_response({"features": []})

        self.assertIsNone(gw.check_coordinates_within_park(*_YELLOWSTONE))

    def test_reads_lowercase_unit_code_field_as_fallback(self):
        gw = _map_gateway()
        gw.session.get.return_value = _json_response({"features": [{"attributes": {"unit_code": "grca"}}]})

        self.assertEqual(gw.check_coordinates_within_park(36.1, -112.1), "grca")

    def test_issues_a_point_intersects_query(self):
        gw = _map_gateway()
        gw.session.get.return_value = _json_response({"features": []})

        gw.check_coordinates_within_park(44.6, -110.5)

        _args, kwargs = gw.session.get.call_args
        params = kwargs["params"]
        self.assertEqual(params["geometryType"], "esriGeometryPoint")
        self.assertEqual(params["spatialRel"], "esriSpatialRelIntersects")
        # ArcGIS point geometry is "longitude,latitude".
        self.assertEqual(params["geometry"], "-110.5,44.6")


class FindParkContainingLocationTests(SimpleTestCase):
    """find_park_containing_location returns details only for a park the point is inside."""

    def test_returns_none_and_skips_lookup_outside_usa(self):
        gw = _parks_gateway()
        with patch.object(NPSMapGateway, "check_coordinates_within_park") as mock_check:
            result = gw.find_park_containing_location(*_PARIS)

        self.assertIsNone(result)
        mock_check.assert_not_called()

    def test_returns_none_when_point_is_in_no_park(self):
        gw = _parks_gateway()
        with (
            patch.object(NPSMapGateway, "check_coordinates_within_park", return_value=None),
            patch.object(NPSGateway, "get_park") as mock_get,
        ):
            result = gw.find_park_containing_location(*_YELLOWSTONE)

        self.assertIsNone(result)
        mock_get.assert_not_called()

    def test_returns_park_details_for_containing_park(self):
        gw = _parks_gateway()
        park = {"fullName": "Yellowstone National Park", "parkCode": "yell"}
        with (
            patch.object(NPSMapGateway, "check_coordinates_within_park", return_value="yell"),
            patch.object(NPSGateway, "get_park", return_value=park) as mock_get,
        ):
            result = gw.find_park_containing_location(*_YELLOWSTONE)

        self.assertEqual(result, park)
        mock_get.assert_called_once_with("yell")

    def test_returns_none_when_boundary_lookup_raises(self):
        gw = _parks_gateway()
        with patch.object(NPSMapGateway, "check_coordinates_within_park", side_effect=RuntimeError("boom")):
            result = gw.find_park_containing_location(*_YELLOWSTONE)

        self.assertIsNone(result)

    def test_returns_none_without_a_traceback_when_rate_limited(self):
        """A live pin-detail view's unpaced lookup can legitimately exceed NPS's
        tight (10/min) budget - this must degrade the same way any other
        boundary-lookup failure does (empty panel, no propagated exception),
        just logged at a level that doesn't read as a crash (see the comment
        at the call site)."""
        from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

        gw = _parks_gateway()
        with (
            patch.object(NPSMapGateway, "check_coordinates_within_park", side_effect=RateLimitExceededError("nps")),
            self.assertLogs("urbanlens.dashboard.services.apis.parks.nps.parks", level="WARNING") as logs,
        ):
            result = gw.find_park_containing_location(*_YELLOWSTONE)

        self.assertIsNone(result)
        self.assertTrue(any("rate limit" in message.lower() for message in logs.output))


class GetParkTests(SimpleTestCase):
    """get_park fetches a single park's detail by park code."""

    def test_returns_first_park_on_success(self):
        gw = _parks_gateway()
        gw.session.get.return_value = _json_response({"data": [{"fullName": "Yellowstone National Park"}]})

        self.assertEqual(gw.get_park("yell"), {"fullName": "Yellowstone National Park"})

    def test_returns_none_when_no_data(self):
        gw = _parks_gateway()
        gw.session.get.return_value = _json_response({"data": []})

        self.assertIsNone(gw.get_park("nope"))

    def test_returns_none_for_blank_code_without_hitting_network(self):
        gw = _parks_gateway()

        self.assertIsNone(gw.get_park(""))
        gw.session.get.assert_not_called()

    def test_returns_none_on_request_exception(self):
        gw = _parks_gateway()
        gw.session.get.side_effect = RuntimeError("network down")

        self.assertIsNone(gw.get_park("yell"))
