"""
Tests for services.apis.locations.weather_resolution - the provider choice for forecast slots, sun times and
recorded weather (mirrors cid_resolution.py's/places_resolution.py's precedent for the same kind of decision).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import WEATHER_PROVIDERS
from urbanlens.dashboard.services.apis.locations import weather_resolution
from urbanlens.dashboard.services.apis.locations.weather_resolution import (
    OpenMeteoProvider,
    OpenWeatherMapProvider,
    WeatherProvider,
)
from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot, SunTimes


def _slot(condition: str) -> ForecastSlot:
    when = datetime(2026, 6, 15, 9, 0)
    return ForecastSlot(
        date=when,
        date_utc=when.replace(tzinfo=UTC),
        temp=70.0,
        condition=condition,
        icon="cloud",
        humidity=None,
        wind_speed=None,
        feels_like=None,
        precipitation_probability=None,
    )


def _sun(hour: int) -> SunTimes:
    return SunTimes(
        sunrise=datetime(2026, 6, 15, hour),
        sunset=datetime(2026, 6, 15, 20),
        golden_hour_morning_end=datetime(2026, 6, 15, hour + 1),
        golden_hour_evening_start=datetime(2026, 6, 15, 19),
    )


class _FakeWeather(WeatherProvider):
    """Answers with canned values (None declines); remembers what it was asked."""

    def __init__(
        self,
        key: str,
        *,
        forecast: list[ForecastSlot] | None = None,
        raw: list[ForecastSlot] | None = None,
        sun: SunTimes | None = None,
        history: dict[str, dict[str, Any]] | None = None,
        is_available: bool = True,
    ) -> None:
        self.key = key
        self.forecast = forecast
        self.raw = raw
        self.sun = sun
        self.history = history
        self.supports_history = history is not None
        self.is_available = is_available
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def forecast_slots(self, latitude: float, longitude: float, *, raw: bool = False) -> list[ForecastSlot] | None:
        self.calls.append("raw" if raw else "forecast")
        return self.raw if raw else self.forecast

    def sun_times(self, latitude: float, longitude: float) -> SunTimes | None:
        self.calls.append("sun")
        return self.sun

    def historical_days(self, latitude: float, longitude: float, start: date, end: date) -> dict[str, dict[str, Any]]:
        self.calls.append("history")
        return self.history or {}


class GetForecastSlotsTests(SimpleTestCase):
    def test_the_first_available_provider_with_a_strip_answers(self) -> None:
        skipped = _FakeWeather("skipped", forecast=[_slot("Skipped")], is_available=False)
        first = _FakeWeather("first", forecast=[_slot("First")])
        second = _FakeWeather("second", forecast=[_slot("Second")])
        with WEATHER_PROVIDERS.overridden([skipped, first, second]):
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        self.assertEqual([slot["condition"] for slot in slots], ["First"])
        self.assertEqual((skipped.calls, second.calls), ([], []))

    def test_a_declined_or_empty_strip_hands_over_to_the_next(self) -> None:
        declines = _FakeWeather("declines", forecast=None)
        empty = _FakeWeather("empty", forecast=[])
        answers = _FakeWeather("answers", forecast=[_slot("Answer")])
        with WEATHER_PROVIDERS.overridden([declines, empty, answers]):
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        self.assertEqual([slot["condition"] for slot in slots], ["Answer"])

    def test_nothing_anywhere_is_an_empty_strip(self) -> None:
        with WEATHER_PROVIDERS.overridden([_FakeWeather("none")]):
            self.assertEqual(weather_resolution.get_forecast_slots(1.0, 2.0), [])
        with WEATHER_PROVIDERS.overridden([]):
            self.assertEqual(weather_resolution.get_forecast_slots(1.0, 2.0), [])


class GetRawForecastSlotsTests(SimpleTestCase):
    def test_an_empty_list_is_an_answer_but_a_decline_is_not(self) -> None:
        declines = _FakeWeather("declines", raw=None)
        empty = _FakeWeather("empty", raw=[])
        later = _FakeWeather("later", raw=[_slot("Later")])
        with WEATHER_PROVIDERS.overridden([declines, empty, later]):
            slots = weather_resolution.get_raw_forecast_slots(1.0, 2.0)

        self.assertEqual(slots, [])
        self.assertEqual(later.calls, [])

    def test_all_declining_is_empty(self) -> None:
        with WEATHER_PROVIDERS.overridden([_FakeWeather("declines", raw=None)]):
            self.assertEqual(weather_resolution.get_raw_forecast_slots(1.0, 2.0), [])


class GetSunTimesTests(SimpleTestCase):
    def test_the_first_provider_with_sun_times_answers(self) -> None:
        none = _FakeWeather("none")
        first = _FakeWeather("first", sun=_sun(5))
        second = _FakeWeather("second", sun=_sun(6))
        with WEATHER_PROVIDERS.overridden([none, first, second]):
            sun_times = weather_resolution.get_sun_times(1.0, 2.0)

        assert sun_times is not None
        self.assertEqual(sun_times["sunrise"], datetime(2026, 6, 15, 5))
        self.assertEqual(second.calls, [])

    def test_nothing_anywhere_is_none(self) -> None:
        with WEATHER_PROVIDERS.overridden([_FakeWeather("none")]):
            self.assertIsNone(weather_resolution.get_sun_times(1.0, 2.0))


class HistoryProvidersTests(SimpleTestCase):
    def test_only_available_providers_that_support_history_are_listed(self) -> None:
        recorder = _FakeWeather("recorder", history={})
        off = _FakeWeather("off", history={}, is_available=False)
        forecaster = _FakeWeather("forecaster")
        with WEATHER_PROVIDERS.overridden([forecaster, off, recorder]):
            self.assertEqual(weather_resolution.history_providers(), [recorder])
            self.assertEqual(weather_resolution.weather_providers(), [forecaster, recorder])

    def test_the_public_providers_record_nothing(self) -> None:
        self.assertFalse(OpenMeteoProvider.supports_history)
        self.assertFalse(OpenWeatherMapProvider.supports_history)


class OpenMeteoProviderTests(SimpleTestCase):
    def test_it_is_always_available_and_serves_the_strip_and_the_raw_form(self) -> None:
        slots = [_slot("Clear")]
        with (
            WEATHER_PROVIDERS.overridden([OpenWeatherMapProvider(), OpenMeteoProvider()]),
            mock.patch.object(weather_resolution.settings, "openweathermap_api_key", None),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast",
                return_value=slots,
            ) as open_meteo,
        ):
            strip = weather_resolution.get_forecast_slots(1.0, 2.0)
            raw = weather_resolution.get_raw_forecast_slots(1.0, 2.0)

        self.assertEqual((strip, raw), (slots, slots))
        self.assertEqual(open_meteo.call_count, 2)

    def test_sun_times_come_from_open_meteo(self) -> None:
        with (
            WEATHER_PROVIDERS.overridden([OpenMeteoProvider()]),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_sun_times",
                return_value=None,
            ) as open_meteo,
        ):
            weather_resolution.get_sun_times(1.0, 2.0)

        open_meteo.assert_called_once()


class OpenWeatherMapProviderTests(SimpleTestCase):
    def test_it_is_available_only_with_a_key(self) -> None:
        with mock.patch.object(weather_resolution.settings, "openweathermap_api_key", "test-key"):
            self.assertTrue(OpenWeatherMapProvider().available())
        with mock.patch.object(weather_resolution.settings, "openweathermap_api_key", None):
            self.assertFalse(OpenWeatherMapProvider().available())

    def test_a_keyed_openweathermap_takes_priority_over_open_meteo(self) -> None:
        raw_item = {"date": datetime(2026, 6, 15, 9, 0), "main": {"temp": 70}, "weather": [{"main": "Clear"}]}
        with (
            WEATHER_PROVIDERS.overridden([OpenWeatherMapProvider(), OpenMeteoProvider()]),
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

    def test_a_failing_openweathermap_hands_over_to_open_meteo(self) -> None:
        with (
            WEATHER_PROVIDERS.overridden([OpenWeatherMapProvider(), OpenMeteoProvider()]),
            mock.patch.object(weather_resolution.settings, "openweathermap_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway.get_weather_forecast",
                side_effect=OSError("boom"),
            ),
            mock.patch(
                "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast",
                return_value=[_slot("Clear")],
            ) as open_meteo,
        ):
            slots = weather_resolution.get_forecast_slots(1.0, 2.0)

        open_meteo.assert_called_once()
        self.assertEqual(len(slots), 1)
