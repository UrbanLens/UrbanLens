"""Tests for RedataNatureObservationsGateway against REData's
``/nature-observations/`` contract (``../REData/docs/api-reference.md``,
"GET /nature-observations/ - recorded wildlife and plants").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_nature_gateway import RedataNatureObservationsGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataNatureObservationsGateway:
    return RedataNatureObservationsGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetNearbyObservationsTests(SimpleTestCase):
    def test_hits_the_nature_observations_path_with_lat_lng(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [
                    {
                        "provider": "inaturalist",
                        "common_name": "Red Fox",
                        "scientific_name": "Vulpes vulpes",
                        "observed_on": "2025-05-01",
                        "uri": "https://x",
                        "coordinate_uncertainty_meters": None,
                        "attributes": {"obscured": False},
                    },
                ],
                "providers": [{"provider": "inaturalist", "status": "ok", "count": 1, "message": None, "radius_meters": 1000.0}],
            },
        )

        envelope = _gateway(session).get_nearby_observations(38.456, -77.123)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/nature-observations/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)
        self.assertFalse(envelope.results[0]["attributes"]["obscured"])

    def test_radius_and_quality_grade_are_sent(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_nearby_observations(1.0, 2.0, radius_meters=2000, quality_grade="research", limit=10)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["radius_meters"], 2000)
        self.assertEqual(params["quality_grade"], "research")
        self.assertEqual(params["limit"], 10)

    def test_omitting_quality_grade_lets_redata_default_to_research(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_nearby_observations(1.0, 2.0)

        self.assertNotIn("quality_grade", session.get.call_args.kwargs["params"])

    def test_obscured_observation_carries_coordinate_uncertainty(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"provider": "inaturalist", "common_name": "Spotted Turtle", "coordinate_uncertainty_meters": 27000, "attributes": {"obscured": True}}],
                "providers": [],
            },
        )

        envelope = _gateway(session).get_nearby_observations(1.0, 2.0)

        self.assertTrue(envelope.results[0]["attributes"]["obscured"])
        self.assertEqual(envelope.results[0]["coordinate_uncertainty_meters"], 27000)
