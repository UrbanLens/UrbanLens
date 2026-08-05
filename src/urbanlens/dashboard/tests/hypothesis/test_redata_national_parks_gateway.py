"""Tests for RedataNationalParksGateway - REData's ``/parks/nearby/`` local NPS catalog lookup.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mock ``session``
(``Gateway.__post_init__`` leaves a non-default session untouched, skipping
the DB-backed rate-limiting wrapper), no database access.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataNationalParksGateway:
    return RedataNationalParksGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class FindParksNearTests(SimpleTestCase):
    def test_hits_the_parks_nearby_path(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/nearby/")

    def test_returns_the_envelopes_results_nearest_first(self) -> None:
        session = mock.Mock()
        units = [{"park_code": "yell", "full_name": "Yellowstone National Park"}]
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": units})

        result = _gateway(session).find_parks_near(44.6, -110.5)

        self.assertEqual(result, units)

    def test_no_providers_block_is_not_required(self) -> None:
        """This endpoint has no ``providers`` block (pure local-catalog read) -
        the envelope parser must not choke on its absence."""
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        result = _gateway(session).find_parks_near(44.6, -110.5)

        self.assertEqual(result, [])

    def test_forwards_radius_and_limit(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5, radius_meters=250_000, limit=5)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["radius_meters"], 250_000)
        self.assertEqual(params["limit"], 5)
        self.assertNotIn("provider", params)

    def test_omitting_radius_lets_redata_use_its_own_default(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5)

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("radius_meters", params)


class FindNearestParkTests(SimpleTestCase):
    def test_returns_the_first_result(self) -> None:
        session = mock.Mock()
        units = [{"park_code": "yell", "full_name": "Yellowstone National Park"}, {"park_code": "grte", "full_name": "Grand Teton National Park"}]
        session.get.return_value = _response(200, {"count": 2, "complete": True, "results": units})

        result = _gateway(session).find_nearest_park(44.6, -110.5)

        self.assertEqual(result, units[0])

    def test_returns_none_when_nothing_is_within_range(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        result = _gateway(session).find_nearest_park(44.6, -110.5)

        self.assertIsNone(result)

    def test_requests_a_limit_of_one(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_nearest_park(44.6, -110.5)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["limit"], 1)
