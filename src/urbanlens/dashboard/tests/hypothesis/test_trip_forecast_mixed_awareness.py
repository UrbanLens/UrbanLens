"""A forecast slot carrying a UTC offset must not 500 the trip page.

`ForecastSlot.date` is parsed with `datetime.fromisoformat`, which passes an
offset straight through, and the three providers behind
`get_raw_forecast_slots` do not agree on a format. REData's is whatever its API
emits, so an aware slot is reachable - and the matching code subtracted it from
a naive target, which raises
`TypeError: can't subtract offset-naive and offset-aware datetimes`.

**This covers the crash only.** The slots are still compared in whatever wall
clock each provider used, and on the Open-Meteo path (the unconditional fallback,
requested with `timezone=auto`) that is local time for the pin while the target
is UTC - a real bug, tracked separately in `docs/PROBLEMS.md`, which needs the
location's timezone to fix properly. These tests deliberately do **not** assert
the *correct* slot is chosen on that path, because it currently isn't.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.trip import _build_activity_forecasts

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
            patch("urbanlens.dashboard.services.apis.locations.weather_resolution.get_raw_forecast_slots", return_value=slots),
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
