"""Tests for services.apis.weather.forecast's provider-shape converters.

Each converter must also honor the ``ForecastSlot`` timezone contract:
``date`` keeps the provider's own wall clock for display, while ``date_utc``
is the same instant as aware UTC (OpenWeatherMap naive = UTC, REData naive
assumed UTC / aware converted, Open-Meteo local anchored by the response's
``utc_offset_seconds``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.weather.forecast import owm_item_to_slot, redata_forecast_to_slots, redata_sun_to_sun_times
from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

#: Fixed-offset zones covering the real UTC-14..UTC+14 range, whole minutes.
_fixed_offsets = st.integers(min_value=-14 * 60, max_value=14 * 60).map(lambda minutes: timezone(timedelta(minutes=minutes)))


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

    @given(st.datetimes())
    def test_naive_date_is_anchored_as_utc(self, date: datetime) -> None:
        """OpenWeatherMap's ``dt_txt`` is UTC, so a naive parse anchors directly."""
        slot = owm_item_to_slot({"date": date, "main": {"temp": 70}, "weather": [{"main": "Clear"}], "wind": {}})

        assert slot is not None
        self.assertEqual(slot["date_utc"], date.replace(tzinfo=UTC))
        self.assertEqual(slot["date_utc"].utcoffset(), timedelta(0))
        self.assertIsNone(slot["date"].tzinfo, "the display date stays naive")

    def test_aware_date_is_converted_to_utc(self) -> None:
        date = datetime(2026, 6, 15, 9, 0, tzinfo=timezone(timedelta(hours=-4)))

        slot = owm_item_to_slot({"date": date, "main": {"temp": 70}, "weather": [{"main": "Clear"}], "wind": {}})

        assert slot is not None
        self.assertEqual(slot["date_utc"], datetime(2026, 6, 15, 13, 0, tzinfo=UTC))


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

    def test_naive_starts_at_is_assumed_utc(self) -> None:
        slots = redata_forecast_to_slots([{"starts_at": "2026-06-15T09:00:00", "temperature_c": 10.0, "condition": "clear"}])

        self.assertEqual(slots[0]["date_utc"], datetime(2026, 6, 15, 9, 0, tzinfo=UTC))
        self.assertIsNone(slots[0]["date"].tzinfo, "the display date stays as REData sent it")

    @given(st.datetimes(min_value=datetime(1980, 1, 1), max_value=datetime(2100, 1, 1), timezones=_fixed_offsets))
    def test_aware_starts_at_round_trips_to_the_same_utc_instant(self, moment: datetime) -> None:
        slots = redata_forecast_to_slots([{"starts_at": moment.isoformat(), "temperature_c": 10.0, "condition": "clear"}])

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["date_utc"], moment.astimezone(UTC))
        self.assertEqual(slots[0]["date_utc"].utcoffset(), timedelta(0))
        self.assertEqual(slots[0]["date"], moment, "the display date keeps the provider's own offset form")


class OpenMeteoDateUtcTests(SimpleTestCase):
    """OpenMeteoGateway anchors its naive-local slots via ``utc_offset_seconds``."""

    @staticmethod
    def _slots_for(payload: dict) -> list | None:
        session = Mock()
        session.get.return_value.raise_for_status.return_value = None
        session.get.return_value.json.return_value = payload
        return OpenMeteoGateway(session=session).get_weather_forecast(42.65, -73.75)

    @staticmethod
    def _payload(offset_seconds: object) -> dict:
        return {
            "utc_offset_seconds": offset_seconds,
            "hourly": {
                "time": ["2026-06-01T09:00", "2026-06-01T18:00"],
                "temperature_2m": [70.0, 75.0],
                "weathercode": [0, 3],
                "relative_humidity_2m": [50, 60],
                "wind_speed_10m": [5.0, 6.0],
            },
        }

    def test_slots_carry_the_offset_anchored_utc_instant(self) -> None:
        slots = self._slots_for(self._payload(-14400))

        assert slots is not None
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0]["date"], datetime(2026, 6, 1, 9, 0), "the display date stays naive local")
        self.assertEqual(slots[0]["date_utc"], datetime(2026, 6, 1, 13, 0, tzinfo=UTC))
        self.assertEqual(slots[1]["date_utc"], datetime(2026, 6, 1, 22, 0, tzinfo=UTC))

    def test_missing_offset_leaves_date_utc_absent(self) -> None:
        payload = self._payload(None)
        del payload["utc_offset_seconds"]

        slots = self._slots_for(payload)

        assert slots is not None
        self.assertNotIn("date_utc", slots[0])

    def test_non_numeric_offset_leaves_date_utc_absent(self) -> None:
        slots = self._slots_for(self._payload("garbage"))

        assert slots is not None
        self.assertNotIn("date_utc", slots[0])

    def test_each_slot_uses_its_own_dates_offset_across_a_dst_transition(self) -> None:
        """One fixed offset cannot anchor a five-day window that crosses a transition.

        US DST ends 2026-11-01, so 10-30 is UTC-4 and 11-02 is UTC-5. Applying
        the single reported ``utc_offset_seconds`` to both put half the
        forecast an hour out, which is enough for ``_build_activity_forecasts``
        to pick the wrong morning or evening slot.
        """
        payload = self._payload(-14400)
        payload["timezone"] = "America/New_York"
        payload["hourly"]["time"] = ["2026-10-30T09:00", "2026-11-02T09:00"]

        slots = self._slots_for(payload)

        assert slots is not None
        self.assertEqual(slots[0]["date_utc"], datetime(2026, 10, 30, 13, 0, tzinfo=UTC))
        self.assertEqual(slots[1]["date_utc"], datetime(2026, 11, 2, 14, 0, tzinfo=UTC))

    def test_an_unrecognized_zone_name_falls_back_to_the_reported_offset(self) -> None:
        payload = self._payload(-14400)
        payload["timezone"] = "Mars/Olympus_Mons"

        slots = self._slots_for(payload)

        assert slots is not None
        self.assertEqual(slots[0]["date_utc"], datetime(2026, 6, 1, 13, 0, tzinfo=UTC))

    @given(st.integers(min_value=-14 * 3600, max_value=14 * 3600))
    def test_date_utc_round_trips_the_payload_offset(self, offset_seconds: int) -> None:
        """For any real-world offset, ``date_utc`` is the local wall clock
        re-anchored in that offset and converted to UTC.
        """
        slots = self._slots_for(self._payload(offset_seconds))

        assert slots is not None
        expected = datetime(2026, 6, 1, 9, 0, tzinfo=timezone(timedelta(seconds=offset_seconds))).astimezone(UTC)
        self.assertEqual(slots[0]["date_utc"], expected)
        self.assertEqual(slots[0]["date"], datetime(2026, 6, 1, 9, 0))


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
