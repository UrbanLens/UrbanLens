"""Tests for RedataHazardsGateway against REData's ``/hazards/`` contract
(``../REData/docs/api-reference.md``, "GET /hazards/ - recorded natural-hazard events").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataHazardsGateway:
    return RedataHazardsGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetHazardEventsTests(SimpleTestCase):
    def test_hits_the_hazards_path_with_lat_lng(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"event_type": "earthquake", "magnitude": 3.2, "magnitude_scale": "Mw", "occurred_at": "2026-01-01T00:00:00Z", "place": "10km N of Nowhere", "url": "https://x"}],
                "providers": [{"provider": "usgs_earthquakes", "status": "ok", "count": 1, "message": None, "radius_meters": 100_000.0}],
            },
        )

        envelope = _gateway(session).get_hazard_events(38.456, -77.123)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/hazards/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)
        self.assertEqual(envelope.results[0]["event_type"], "earthquake")

    def test_radius_min_magnitude_and_years_are_sent(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_hazard_events(1.0, 2.0, radius_meters=100_000, min_magnitude=3.0, years=10, limit=10)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["radius_meters"], 100_000)
        self.assertEqual(params["min_magnitude"], 3.0)
        self.assertEqual(params["years"], 10)
        self.assertEqual(params["limit"], 10)

    def test_omitting_min_magnitude_and_years_omits_them_from_the_request(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_hazard_events(1.0, 2.0)

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("min_magnitude", params)
        self.assertNotIn("years", params)
        self.assertNotIn("radius_meters", params)
