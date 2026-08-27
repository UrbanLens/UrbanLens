"""Tests for RedataGeocodeGateway against REData's shipped contract
(``../REData/docs/api-reference.md``, "GET /geocode/" and "GET /geocode/reverse/").
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_geocode_gateway import RedataGeocodeGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataGeocodeGateway:
    return RedataGeocodeGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GeocodeTests(SimpleTestCase):
    def test_sends_query_and_optional_bias_coordinates(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": [{"provider": "nominatim", "latitude": 1.0, "longitude": 2.0}], "providers": []})

        envelope = _gateway(session).geocode("123 Main St", latitude=1.0, longitude=2.0, limit=1)

        self.assertEqual(envelope.results[0]["provider"], "nominatim")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "123 Main St")
        self.assertEqual(params["lat"], 1.0)
        self.assertEqual(params["lng"], 2.0)
        self.assertEqual(params["limit"], 1)

    def test_omits_bias_coordinates_when_not_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).geocode("some place")

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("lat", params)
        self.assertNotIn("lng", params)


class ReverseGeocodeTests(SimpleTestCase):
    def test_sends_coordinates(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": [{"provider": "nominatim"}], "providers": []})

        envelope = _gateway(session).reverse_geocode(38.456, -77.123, provider="nominatim")

        self.assertEqual(len(envelope.results), 1)
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/geocode/reverse/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)
        self.assertEqual(params["provider"], "nominatim")
