"""Tests for services.apis.locations.weather_resolution - the REData-vs-direct
provider choice for forecast slots and sun times (mirrors cid_resolution.py's/
places_resolution.py's precedent for the same kind of decision).
"""

from __future__ import annotations

from datetime import datetime
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import weather_resolution
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

_MODULE = "urbanlens.dashboard.services.apis.locations.weather_resolution"


class GetForecastSlotsTests(SimpleTestCase):
    def test_redata_configured_prefers_openweathermap_entry(self) -> None:
        results = [
            {
                "provider": "open_meteo",
                "forecast": [
                    {
                        "granularity": "hourly",
                        "starts_at": "2026-06-15T09:00:00",
                        "temperature_c": 10.0,
                        "condition": "clear",
                    }
                ],
            },
            {
                "provider": "openweathermap",
                "forecast": [
                    {
                        "granularity": "hourly",
                        "starts_at": "2026-06-15T09:00:00",
                        "temperature_c": 20.0,
                        "condition": "rain",
                    }
                ],
            },
        ]
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataWeatherGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_weather.return_value = results
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["condition"], "Rain")

    def test_redata_configured_falls_back_to_open_meteo_entry_when_owm_empty(self) -> None:
        results = [
            {
                "provider": "open_meteo",
                "forecast": [
                    {
                        "granularity": "hourly",
                        "starts_at": "2026-06-15T09:00:00",
                        "temperature_c": 10.0,
                        "condition": "clear",
                    }
                ],
            },
            {"provider": "openweathermap", "forecast": []},
        ]
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataWeatherGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_weather.return_value = results
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["condition"], "Clear")

    def test_redata_not_configured_uses_direct_chain(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch.object(weather_resolution.settings, "openweathermap_api_key", None),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast",
                return_value=[],
            ) as open_meteo,
        ):
            weather_resolution.get_forecast_slots(1.0, 2.0)

        open_meteo.assert_called_once()

    def test_redata_failure_falls_back_to_direct_chain(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataWeatherGateway") as gateway_cls,
            mock.patch.object(weather_resolution.settings, "openweathermap_api_key", None),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast",
                return_value=[],
            ) as open_meteo,
        ):
            gateway_cls.return_value.get_weather.side_effect = LocationContextUnavailableError("source_error", "boom")
            weather_resolution.get_forecast_slots(1.0, 2.0)

        open_meteo.assert_called_once()

    def test_openweathermap_configured_takes_priority_over_open_meteo_on_direct_path(self) -> None:
        raw_item = {"date": datetime(2026, 6, 15, 9, 0), "main": {"temp": 70}, "weather": [{"main": "Clear"}]}
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch.object(weather_resolution.settings, "openweathermap_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway.get_weather_forecast",
                return_value=[raw_item],
            ),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast"
            ) as open_meteo,
        ):
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        open_meteo.assert_not_called()
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["condition"], "Clear")


class GetSunTimesTests(SimpleTestCase):
    def test_redata_configured_prefers_open_meteo_entry(self) -> None:
        results = [
            {"provider": "openweathermap", "sun": {}},
            {
                "provider": "open_meteo",
                "sun": {
                    "sunrise": "2026-06-15T05:32:00",
                    "sunset": "2026-06-15T20:47:00",
                    "golden_hour_morning_end": "2026-06-15T06:32:00",
                    "golden_hour_evening_start": "2026-06-15T19:47:00",
                },
            },
        ]
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=True),
            mock.patch(f"{_MODULE}.RedataWeatherGateway") as gateway_cls,
        ):
            gateway_cls.return_value.get_weather.return_value = results
            sun_times = weather_resolution.get_sun_times(1.0, 2.0)

        assert sun_times is not None
        self.assertEqual(sun_times["sunrise"], datetime(2026, 6, 15, 5, 32))

    def test_redata_not_configured_uses_open_meteo_directly(self) -> None:
        with (
            mock.patch(f"{_MODULE}.redata_configured", return_value=False),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_sun_times", return_value=None
            ) as open_meteo,
        ):
            weather_resolution.get_sun_times(1.0, 2.0)

        open_meteo.assert_called_once()
