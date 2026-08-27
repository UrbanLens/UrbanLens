"""Tests for RedataElevationGateway against REData's ``/elevation/`` contract
(``../REData/docs/api-reference.md``, "GET /elevation/ - metres above sea level").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_elevation_gateway import RedataElevationGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataElevationGateway:
    return RedataElevationGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetElevationTests(SimpleTestCase):
    def test_hits_the_elevation_path_with_lat_lng(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"provider": "usgs_epqs", "dataset": "3DEP", "resolution_meters": 10, "elevation_meters": 245.0, "status": "ok"}],
                "providers": [],
            },
        )

        envelope = _gateway(session).get_elevation(38.456, -77.123)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/elevation/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)
        self.assertNotIn("radius_meters", params)
        self.assertEqual(envelope.results[0]["elevation_meters"], 245.0)

    def test_returns_every_configured_dem_reading_not_one_winner(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 3,
                "complete": True,
                "results": [
                    {"provider": "usgs_epqs", "dataset": "3DEP", "resolution_meters": 10, "elevation_meters": 245.0, "status": "ok"},
                    {"provider": "open_elevation", "dataset": "SRTM", "resolution_meters": 90, "elevation_meters": 240.0, "status": "ok"},
                    {"provider": "open_meteo", "dataset": "Copernicus DEM GLO-90", "resolution_meters": 90, "elevation_meters": None, "status": "ok"},
                ],
                "providers": [],
            },
        )

        envelope = _gateway(session).get_elevation(1.0, 2.0)

        self.assertEqual(envelope.count, 3)
        self.assertEqual(len(envelope.results), 3)
        # A null reading with a real status is a genuine answer, not dropped.
        self.assertIsNone(envelope.results[2]["elevation_meters"])

    def test_force_refresh_is_sent(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_elevation(1.0, 2.0, force_refresh=True)

        self.assertEqual(session.get.call_args.kwargs["params"]["force_refresh"], "true")
