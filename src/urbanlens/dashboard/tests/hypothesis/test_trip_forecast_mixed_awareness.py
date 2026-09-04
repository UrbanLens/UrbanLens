"""Trip activity/forecast matching across the providers' timezone shapes.

`ForecastSlot.date` is the provider's own wall clock with no timezone
contract (Open-Meteo's is local to the pin, OpenWeatherMap's is UTC, REData's
is whatever its API emits), so two distinct behaviors are covered here:

- **The crash guard** (`MixedAwarenessForecastTests`): a slot whose `date`
  carries an offset must not raise
  `TypeError: can't subtract offset-naive and offset-aware datetimes`
  and 500 the trip page - both sides of the fallback wall-clock comparison
  are forced naive.
- **Offset-correct matching** (`UtcAnchoredSlotMatchingTests`): slots
  carrying the aware-UTC `date_utc` (Open-Meteo anchors its local wall
  clocks with the response's `utc_offset_seconds`) are compared against the
  aware `scheduled_at` directly, so the chosen slot matches the activity's
  UTC instant rather than being offset by the location's UTC offset.
"""

from __future__ import annotations

import datetime
from unittest.mock import Mock, patch

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.trip import _build_activity_forecasts
from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

_COORDS = (42.6526, -73.7562)


class MixedAwarenessForecastTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.scheduled = timezone.make_aware(datetime.datetime(2026, 6, 1, 12, 0))
        trip = baker.make("dashboard.Trip")
        self.activity = baker.make("dashboard.TripActivity", trip=trip, scheduled_at=self.scheduled)

    def _forecasts(self, slots: list[dict]) -> list[dict]:
        with (
            patch("urbanlens.dashboard.controllers.trip.activity_coords", return_value=_COORDS),
            patch(
                "urbanlens.dashboard.services.apis.locations.weather_resolution.get_raw_forecast_slots",
                return_value=slots,
            ),
        ):
            return _build_activity_forecasts([self.activity])

    def test_an_offset_carrying_slot_does_not_raise(self) -> None:
        """The REData-shaped case: fromisoformat preserved a +00:00 offset."""
        aware_slot = {"date": datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.UTC), "temp": 20}

        results = self._forecasts([aware_slot])

        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0]["slot"], "an in-window slot should still be matched")

    def test_naive_slots_still_match(self) -> None:
        """The ordinary path must be unaffected."""
        naive_slot = {"date": datetime.datetime(2026, 6, 1, 13, 0), "temp": 20}

        results = self._forecasts([naive_slot])

        self.assertIs(results[0]["slot"], naive_slot)

    def test_a_mix_of_aware_and_naive_slots_does_not_raise(self) -> None:
        """Providers can change shape between cache entries; the min() must survive it."""
        slots = [
            {"date": datetime.datetime(2026, 6, 3, 12, 0, tzinfo=datetime.UTC), "temp": 1},
            {"date": datetime.datetime(2026, 6, 1, 13, 0), "temp": 2},
        ]

        results = self._forecasts(slots)

        self.assertIs(results[0]["slot"], slots[1], "the nearer naive slot wins")

    def test_an_aware_slot_far_outside_the_window_is_still_flagged(self) -> None:
        """The out-of-range test has to survive the same subtraction."""
        far = {"date": datetime.datetime(2026, 6, 20, 12, 0, tzinfo=datetime.UTC), "temp": 20}

        results = self._forecasts([far])

        self.assertTrue(results[0]["out_of_range"])
        self.assertIsNone(results[0]["slot"])


class UtcAnchoredSlotMatchingTests(TestCase):
    """Slots carrying `date_utc` are matched by UTC instant, not wall clock."""

    def _forecasts_for(self, scheduled_at: datetime.datetime, slots: list[dict]) -> list[dict]:
        trip = baker.make("dashboard.Trip")
        activity = baker.make("dashboard.TripActivity", trip=trip, scheduled_at=scheduled_at)
        with (
            patch("urbanlens.dashboard.controllers.trip.activity_coords", return_value=_COORDS),
            patch(
                "urbanlens.dashboard.services.apis.locations.weather_resolution.get_raw_forecast_slots",
                return_value=slots,
            ),
        ):
            return _build_activity_forecasts([activity])

    def test_open_meteo_payload_matches_the_activitys_utc_instant(self) -> None:
        """End-to-end through the Open-Meteo converter: `timezone=auto` gives
        local wall clocks, and the response's non-zero `utc_offset_seconds`
        (UTC-4 here, a US-Eastern summer offset) anchors them.

        The activity is at 15:00 UTC = 11:00 local. In UTC the 09:00-local
        slot (13:00 UTC) is 2h away and the 18:00-local slot (22:00 UTC) is
        7h away; by raw wall clock the gaps invert (6h vs 3h), so the
        pre-`date_utc` comparison picked the wrong slot.
        """
        payload = {
            "utc_offset_seconds": -14400,
            "hourly": {
                "time": ["2026-06-01T09:00", "2026-06-01T18:00"],
                "temperature_2m": [70.0, 75.0],
                "weathercode": [0, 3],
                "relative_humidity_2m": [50, 60],
                "wind_speed_10m": [5.0, 6.0],
            },
        }
        session = Mock()
        session.get.return_value.raise_for_status.return_value = None
        session.get.return_value.json.return_value = payload
        slots = OpenMeteoGateway(session=session).get_weather_forecast(*_COORDS)
        assert slots is not None

        results = self._forecasts_for(datetime.datetime(2026, 6, 1, 15, 0, tzinfo=datetime.UTC), list(slots))

        slot = results[0]["slot"]
        self.assertIsNotNone(slot)
        self.assertEqual(
            slot["date"],
            datetime.datetime(2026, 6, 1, 9, 0),
            "the 13:00 UTC slot wins, displayed as its local wall clock",
        )
        self.assertEqual(slot["date_utc"], datetime.datetime(2026, 6, 1, 13, 0, tzinfo=datetime.UTC))

    def test_gap_hours_uses_the_utc_instant_for_the_out_of_range_test(self) -> None:
        """A slot 32h away in UTC but 45h away by wall clock (UTC+13) must be
        matched, not flagged out of range by the local wall-clock gap.
        """
        slot = {
            "date": datetime.datetime(2026, 6, 3, 9, 0),
            "date_utc": datetime.datetime(2026, 6, 2, 20, 0, tzinfo=datetime.UTC),
            "temp": 20,
        }

        results = self._forecasts_for(datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.UTC), [slot])

        self.assertFalse(results[0]["out_of_range"])
        self.assertIs(results[0]["slot"], slot)

    def test_mixed_anchored_and_unanchored_slots_each_use_their_own_metric(self) -> None:
        """An anchored slot competes on its UTC gap while an unanchored one
        keeps the naive wall-clock gap, in the same `min()`.
        """
        unanchored = {"date": datetime.datetime(2026, 6, 1, 11, 0), "temp": 1}  # 1h naive gap
        anchored = {
            "date": datetime.datetime(2026, 6, 1, 8, 30),  # 3.5h naive gap - would lose under wall clock
            "date_utc": datetime.datetime(2026, 6, 1, 12, 30, tzinfo=datetime.UTC),  # 0.5h UTC gap
            "temp": 2,
        }

        results = self._forecasts_for(datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.UTC), [unanchored, anchored])

        self.assertIs(results[0]["slot"], anchored)
