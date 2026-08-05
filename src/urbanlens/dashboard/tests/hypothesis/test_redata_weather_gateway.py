"""Tests for RedataWeatherGateway against REData's shipped contract
(``../REData/docs/api-reference.md``, "GET /weather/ - conditions, forecast and sun times for a point").
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_weather_gateway import RedataWeatherGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataWeatherGateway:
    return RedataWeatherGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetWeatherTests(SimpleTestCase):
    def test_returns_every_provider_entry(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 2,
                "complete": True,
                "results": [
                    {"provider": "open_meteo", "current": {"temperature_c": 20.0}, "forecast": [], "sun": {"sunrise": "2026-06-15T05:32:00"}},
                    {"provider": "openweathermap", "current": {"temperature_c": 21.0}, "forecast": [], "sun": {}},
                ],
                "providers": [],
            },
        )

        results = _gateway(session).get_weather(38.456, -77.123)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["provider"], "open_meteo")
        self.assertEqual(results[1]["provider"], "openweathermap")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)

    def test_empty_results_returns_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        self.assertEqual(_gateway(session).get_weather(1.0, 2.0), [])
