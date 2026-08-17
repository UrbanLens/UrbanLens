"""Open-Meteo forecast slots must come out as UTC, not local wall clock.

``OpenMeteoGateway`` asks for ``timezone=auto``, so every ``hourly.time`` it
gets back is a local wall clock for the pin's coordinates. ``ForecastSlot.date``
is compared against ``Activity.scheduled_at``, which is stored UTC, and against
nothing else - it is never rendered. Emitting local time therefore made that
comparison local-minus-UTC: out by the location's offset, four or five hours in
New York, nine in Tokyo. The visible effects were a wrong "closest" slot and an
out-of-range test (``gap_hours > 36``) skewed by the same amount.

Open-Meteo reports the offset it applied, as top-level ``utc_offset_seconds``,
so the correction needs no timezone database and no change to the request - the
provider keeps resolving local time for everything that wants it, and only the
slot dates are converted on the way out.

`docs/PROBLEMS.md` recorded this as needing an owner's decision, on the reading
that a correct comparison "genuinely cannot be built from what is already here"
and that switching to UTC would change what users see. Both premises were
checked again here: the offset *is* already in the response, and
``ForecastSlot["date"]`` has exactly two readers, both of them the matching
arithmetic in ``controllers/trip.py``.
"""

from __future__ import annotations

from datetime import datetime
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

#: 09:00 and 18:00 local, the two slots the gateway keeps.
_HOURLY = {
    "time": ["2026-08-20T09:00", "2026-08-20T18:00"],
    "temperature_2m": [18.0, 24.0],
    "weathercode": [0, 0],
    "relative_humidity_2m": [50, 40],
    "wind_speed_10m": [5.0, 6.0],
}


def _slots(payload: dict) -> list:
    gateway = OpenMeteoGateway()
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    with mock.patch.object(gateway, "session") as session:
        session.get.return_value = response
        return gateway.get_weather_forecast(40.7, -74.0) or []


class OpenMeteoSlotTimezoneTests(SimpleTestCase):
    def test_local_slot_times_are_converted_to_utc(self) -> None:
        """New York in August is UTC-4, so 09:00 local is 13:00 UTC."""
        slots = _slots({"hourly": _HOURLY, "utc_offset_seconds": -4 * 3600})

        self.assertEqual([slot["date"] for slot in slots], [datetime(2026, 8, 20, 13, 0), datetime(2026, 8, 20, 22, 0)])

    def test_a_positive_offset_converts_the_other_way(self) -> None:
        """Tokyo is UTC+9, so 09:00 local is 00:00 UTC the same day."""
        slots = _slots({"hourly": _HOURLY, "utc_offset_seconds": 9 * 3600})

        self.assertEqual([slot["date"] for slot in slots], [datetime(2026, 8, 20, 0, 0), datetime(2026, 8, 20, 9, 0)])

    def test_a_response_without_the_offset_is_unchanged(self) -> None:
        """Defaulting to zero keeps a provider that omits the field behaving as before."""
        slots = _slots({"hourly": _HOURLY})

        self.assertEqual([slot["date"] for slot in slots], [datetime(2026, 8, 20, 9, 0), datetime(2026, 8, 20, 18, 0)])

    def test_the_other_slot_fields_are_untouched(self) -> None:
        """The conversion must move the clock and nothing else."""
        slots = _slots({"hourly": _HOURLY, "utc_offset_seconds": -4 * 3600})

        self.assertEqual([slot["temp"] for slot in slots], [18.0, 24.0])
        self.assertEqual([slot["humidity"] for slot in slots], [50, 40])
