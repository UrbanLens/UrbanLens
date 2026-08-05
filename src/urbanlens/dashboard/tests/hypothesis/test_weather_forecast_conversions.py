"""Tests for services.apis.weather.forecast's provider-shape converters."""

from __future__ import annotations

from datetime import datetime

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.weather.forecast import owm_item_to_slot, redata_forecast_to_slots, redata_sun_to_sun_times


class OwmItemToSlotTests(SimpleTestCase):
    def test_extracts_feels_like_and_precipitation_probability(self) -> None:
        item = {
            "date": datetime(2026, 6, 15, 9, 0),
            "main": {"temp": 70, "feels_like": 68, "humidity": 50},
            "weather": [{"main": "Rain"}],
            "wind": {"speed": 5},
            "pop": 0.4,
        }

        slot = owm_item_to_slot(item)

        assert slot is not None
        self.assertEqual(slot["feels_like"], 68)
        self.assertEqual(slot["precipitation_probability"], 40)

    def test_missing_pop_gives_none_probability(self) -> None:
        item = {"date": datetime(2026, 6, 15, 9, 0), "main": {"temp": 70}, "weather": [{"main": "Clear"}], "wind": {}}

        slot = owm_item_to_slot(item)

        assert slot is not None
        self.assertIsNone(slot["precipitation_probability"])

    def test_missing_date_returns_none(self) -> None:
        self.assertIsNone(owm_item_to_slot({"main": {"temp": 70}}))


class RedataForecastToSlotsTests(SimpleTestCase):
    def test_converts_hourly_entry(self) -> None:
        forecast = [
            {
                "granularity": "hourly",
                "starts_at": "2026-06-15T09:00:00",
                "temperature_c": 21.0,
                "apparent_temperature_c": 19.0,
                "relative_humidity_percent": 55,
                "wind_speed_ms": 4.0,
                "precipitation_probability_percent": 30,
                "condition": "partly_cloudy",
            },
        ]

        slots = redata_forecast_to_slots(forecast)

        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot["date"], datetime(2026, 6, 15, 9, 0))
        self.assertAlmostEqual(slot["temp"], 69.8, places=1)
        self.assertEqual(slot["condition"], "Partly Cloudy")
        self.assertEqual(slot["icon"], "partly_cloudy_day")
        self.assertEqual(slot["humidity"], 55)
        self.assertAlmostEqual(slot["wind_speed"], 8.947744, places=3)
        self.assertAlmostEqual(slot["feels_like"], 66.2, places=1)
        self.assertEqual(slot["precipitation_probability"], 30)

    def test_daily_entry_uses_temperature_max(self) -> None:
        forecast = [{"granularity": "daily", "starts_at": "2026-06-16T00:00:00", "temperature_max_c": 25.0, "condition": "overcast"}]

        slots = redata_forecast_to_slots(forecast)

        self.assertEqual(len(slots), 1)
        self.assertAlmostEqual(slots[0]["temp"], 77.0, places=1)

    def test_entry_missing_starts_at_is_skipped(self) -> None:
        self.assertEqual(redata_forecast_to_slots([{"condition": "clear"}]), [])

    def test_unknown_condition_maps_to_cloud_icon(self) -> None:
        forecast = [{"starts_at": "2026-06-15T09:00:00", "temperature_c": 10.0, "condition": "hail"}]

        slots = redata_forecast_to_slots(forecast)

        self.assertEqual(slots[0]["icon"], "cloud")
        self.assertEqual(slots[0]["condition"], "Hail")


class RedataSunToSunTimesTests(SimpleTestCase):
    def test_converts_full_sun_block(self) -> None:
        sun = {
            "sunrise": "2026-06-15T05:32:00",
            "sunset": "2026-06-15T20:47:00",
            "golden_hour_morning_end": "2026-06-15T06:32:00",
            "golden_hour_evening_start": "2026-06-15T19:47:00",
        }

        sun_times = redata_sun_to_sun_times(sun)

        assert sun_times is not None
        self.assertEqual(sun_times["sunrise"], datetime(2026, 6, 15, 5, 32))
        self.assertEqual(sun_times["sunset"], datetime(2026, 6, 15, 20, 47))

    def test_empty_block_returns_none(self) -> None:
        self.assertIsNone(redata_sun_to_sun_times({}))

    def test_partial_block_returns_none(self) -> None:
        self.assertIsNone(redata_sun_to_sun_times({"sunrise": "2026-06-15T05:32:00"}))
