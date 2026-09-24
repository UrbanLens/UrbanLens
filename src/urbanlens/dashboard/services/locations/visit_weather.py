"""What the weather actually was on the day of a visit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: A grid cell, in hundredths of a degree: ``(latitude, longitude)``. ERA5's own grid is 0.25°, so a
#: 0.01° cell loses nothing and lets nearby places share recorded days.
Cell = tuple[int, int]

#: How long a queued fetch for the same cell and days is not queued again.
_QUEUE_DEDUPE_SECONDS = 600

#: ERA5 begins in 1940; REData clamps rather than rejects, so an earlier date
#: would cost a request that can only come back empty.
RECORD_BEGINS = date(1940, 1, 1)

#: How far behind real time ERA5 runs. REData documents about six days; the
#: extra day keeps a boundary case from asking for a day that does not exist
#: yet just because of a timezone difference.
PUBLICATION_LAG_DAYS = 7


def is_recorded_yet(day: date, *, today: date | None = None) -> bool:
    """Whether ERA5 can be expected to hold a record for ``day``.

    Args:
        day: The day in question.
        today: Override for the current date, for tests.

    Returns:
        False when ``day`` predates :data:`RECORD_BEGINS` or falls inside the :data:`PUBLICATION_LAG_DAYS` window - in both cases there is nothing to fetch, and the second is temporary."""
    current = today or timezone.localdate()
    return RECORD_BEGINS <= day <= current - timedelta(days=PUBLICATION_LAG_DAYS)


def weather_cell(latitude: float, longitude: float) -> Cell:
    """The cell a coordinate's recorded days are stored under.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The cell.
    """
    return round(float(latitude) * 100), round(float(longitude) * 100)


def location_cell(location: Location) -> Cell:
    """The cell a Location's recorded days are stored under.

    Args:
        location: The shared Location.

    Returns:
        The cell.
    """
    return weather_cell(float(location.latitude), float(location.longitude))


def cached_records(wanted: Mapping[Cell, Iterable[date]]) -> dict[Cell, dict[str, dict[str, Any]]]:
    """The stored rows for several cells' days, in one query.

    Args:
        wanted: The days wanted in each cell.

    Returns:
        ``{cell: {iso_date: record}}`` for the days already stored.
    """
    from django.db.models import Q

    from urbanlens.dashboard.models.cache.recorded_weather import RecordedWeatherDay

    condition = Q()
    for (cell_lat, cell_lng), days in wanted.items():
        day_list = sorted(set(days))
        if day_list:
            condition |= Q(cell_lat=cell_lat, cell_lng=cell_lng, day__in=day_list)
    found: dict[Cell, dict[str, dict[str, Any]]] = {cell: {} for cell in wanted}
    if not condition:
        return found
    for cell_lat, cell_lng, day, data in RecordedWeatherDay.objects.filter(condition).values_list("cell_lat", "cell_lng", "day", "data"):
        if isinstance(data, dict):
            found.setdefault((cell_lat, cell_lng), {})[day.isoformat()] = data
    return found


@dataclass(slots=True, frozen=True)
class RecordedDay:
    """One recorded day, converted to the units the rest of the app displays.
    Converting here rather than in the template keeps one implementation and one rounding decision.

    Attributes:
        day: The day these readings describe.
        high_f: Daily maximum temperature, degrees Fahrenheit.
        low_f: Daily minimum temperature.
        mean_f: Daily mean temperature.
        precipitation_in: Rainfall as liquid volume, inches.
        snowfall_in: Snow as accumulated depth, inches - not the same quantity as ``precipitation_in`` in different units.
        wind_max_mph: Maximum sustained wind.
        gust_max_mph: Maximum gust."""

    day: date
    high_f: float | None = None
    low_f: float | None = None
    mean_f: float | None = None
    precipitation_in: float | None = None
    snowfall_in: float | None = None
    wind_max_mph: float | None = None
    gust_max_mph: float | None = None

    @property
    def has_readings(self) -> bool:
        """Whether anything at all came back for this day."""
        return any(value is not None for value in (self.high_f, self.low_f, self.mean_f, self.precipitation_in, self.snowfall_in, self.wind_max_mph, self.gust_max_mph))

    @property
    def summary(self) -> str:
        """A one-line description of the day, for a visit or memory row.
        Assembled here rather than in a template because the interesting cases are all conditional - a day with a high but no low, a dry day, a reading of exactly zero - and each one is a branch a template expresses badly and nothing can test.

        Returns:
            Something like ``"72° / 54°F · 0.30 in rain · gusts 31 mph"``, or ``""`` when nothing came back."""
        parts: list[str] = []
        if self.high_f is not None and self.low_f is not None:
            parts.append(f"{self.high_f:.0f}° / {self.low_f:.0f}°F")
        elif self.high_f is not None:
            parts.append(f"high {self.high_f:.0f}°F")
        elif self.low_f is not None:
            parts.append(f"low {self.low_f:.0f}°F")
        elif self.mean_f is not None:
            parts.append(f"{self.mean_f:.0f}°F")
        if self.precipitation_in:
            parts.append(f"{self.precipitation_in:.2f} in rain")
        if self.snowfall_in:
            parts.append(f"{self.snowfall_in:.1f} in snow")
        if self.gust_max_mph:
            parts.append(f"gusts {self.gust_max_mph:.0f} mph")
        elif self.wind_max_mph:
            parts.append(f"wind {self.wind_max_mph:.0f} mph")
        return " · ".join(parts)


def _f(celsius: Any) -> float | None:
    return round(celsius * 9 / 5 + 32, 1) if isinstance(celsius, (int, float)) else None


def _inches_from_mm(mm: Any) -> float | None:
    return round(mm / 25.4, 2) if isinstance(mm, (int, float)) else None


def _inches_from_cm(cm: Any) -> float | None:
    return round(cm / 2.54, 2) if isinstance(cm, (int, float)) else None


def _mph(kmh: Any) -> float | None:
    return round(kmh * 0.621371, 1) if isinstance(kmh, (int, float)) else None


def to_recorded_day(record: dict[str, Any]) -> RecordedDay | None:
    """Convert one REData ``/weather/history/`` row into display units.

    Args:
        record: A row as returned by :meth:`RedataWeatherHistoryGateway.get_history`.

    Returns:
        The converted day, or None when the row carries no parseable date.
    """
    raw = record.get("date")
    try:
        day = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return RecordedDay(
        day=day,
        high_f=_f(record.get("temperature_max_c")),
        low_f=_f(record.get("temperature_min_c")),
        mean_f=_f(record.get("temperature_mean_c")),
        precipitation_in=_inches_from_mm(record.get("precipitation_mm")),
        snowfall_in=_inches_from_cm(record.get("snowfall_cm")),
        wind_max_mph=_mph(record.get("wind_speed_max_kmh")),
        gust_max_mph=_mph(record.get("wind_gusts_max_kmh")),
    )


def _fetch_days(latitude: float, longitude: float, start: date, end: date) -> dict[str, Any]:
    """Ask REData for a date range at a point, keyed by ISO date.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        start: First day, inclusive.
        end: Last day, inclusive.

    Returns:
        ``{iso_date: record}`` for the days REData could answer, empty when it could not answer at all."""
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured

    if not redata_configured():
        return {}

    from urbanlens.dashboard.services.apis.locations.redata_weather_gateway import RedataWeatherHistoryGateway

    try:
        days = RedataWeatherHistoryGateway().get_history(latitude, longitude, start=start, end=end)
    except LocationContextUnavailableError as exc:
        logger.info(
            "Historical weather unavailable for %s,%s %s..%s: %s",
            redact_coordinate(latitude),
            redact_coordinate(longitude),
            start,
            end,
            exc.reason,
        )
        return {}
    return {str(entry["date"]): entry for entry in days if isinstance(entry, dict) and entry.get("date")}


#: How far apart two missing days can be and still be fetched as one range.
#: A range is one request however wide it is, so merging is nearly free - up to the point where the
#: answer itself is not.
_MERGE_GAP_DAYS = 31


def _clusters(days: list[date]) -> list[tuple[date, date]]:
    """Group sorted days into ranges worth fetching in one request.

    Args:
        days: The days to fetch, in any order.

    Returns:
        ``(start, end)`` pairs covering every day, splitting wherever the gap exceeds :data:`_MERGE_GAP_DAYS`."""
    ordered = sorted(set(days))
    if not ordered:
        return []
    clusters: list[tuple[date, date]] = []
    start = previous = ordered[0]
    for day in ordered[1:]:
        if (day - previous).days > _MERGE_GAP_DAYS:
            clusters.append((start, previous))
            start = day
        previous = day
    clusters.append((start, previous))
    return clusters


def convert_records(records: Mapping[str, Any], wanted: Iterable[date]) -> dict[str, RecordedDay]:
    """Stored records for the wanted days, in display units.

    Args:
        records: ``{iso_date: record}`` as :func:`cached_records` returns them for one cell.
        wanted: The days to convert.

    Returns:
        ``{iso_date: RecordedDay}`` for the wanted days that have a parseable record.
    """
    converted: dict[str, RecordedDay] = {}
    for day in wanted:
        record = records.get(day.isoformat())
        if isinstance(record, dict) and (recorded := to_recorded_day(record)) is not None:
            converted[day.isoformat()] = recorded
    return converted


def recorded_days_at(latitude: float, longitude: float, days: Iterable[date], *, allow_fetch: bool = True) -> dict[str, RecordedDay]:
    """Recorded weather for a set of days at a coordinate, from stored rows first.

    Missing days are fetched in clustered ranges, so days years apart never become one request for
    every day between them.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        days: The days wanted, in any order.
        allow_fetch: When False, answer only from stored rows.

    Returns:
        ``{iso_date: RecordedDay}`` for the days that could be answered."""
    wanted = sorted({day for day in days if is_recorded_yet(day)})
    if not wanted:
        return {}
    cell = weather_cell(latitude, longitude)
    records = cached_records({cell: wanted})[cell]
    if allow_fetch:
        for start, end in _clusters(missing_days(wanted, records)):
            fetched = _fetch_days(cell[0] / 100, cell[1] / 100, start, end)
            if fetched:
                _store(cell, fetched)
                records = {**records, **fetched}
    return convert_records(records, wanted)


def recorded_days(location: Location, days: Iterable[date], *, allow_fetch: bool = True) -> dict[str, RecordedDay]:
    """:func:`recorded_days_at` for a Location.

    Args:
        location: The shared Location.
        days: The days wanted, in any order.
        allow_fetch: When False, answer only from stored rows.

    Returns:
        ``{iso_date: RecordedDay}`` for the days that could be answered."""
    return recorded_days_at(float(location.latitude), float(location.longitude), days, allow_fetch=allow_fetch)


def missing_days(days: Iterable[date], records: Mapping[str, Any]) -> list[date]:
    """Which of ``days`` are recordable and not stored yet.

    Args:
        days: The days wanted, in any order.
        records: One cell's stored records, as :func:`cached_records` returns them.

    Returns:
        The days worth fetching, sorted."""
    return sorted({day for day in days if is_recorded_yet(day) and day.isoformat() not in records})


def queue_missing_days(cell: Cell, days: Iterable[date]) -> bool:
    """Queue a background fetch of a cell's missing days, unless the same fetch was queued recently.

    A page that shows recorded weather reads stored rows only and calls this for the rest, so a slow
    REData never holds up a render.

    Args:
        cell: The cell.
        days: Its missing days.

    Returns:
        Whether a fetch was queued; never on an install with no REData to ask.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import fetch_recorded_weather_at

    iso_days = sorted({day.isoformat() for day in days})
    if not iso_days or not redata_configured():
        return False
    digest = hashlib.sha256(",".join(iso_days).encode()).hexdigest()[:24]
    try:
        first = bool(cache.add(f"ul:weather-history-queued:{cell[0]}:{cell[1]}:{digest}", 1, _QUEUE_DEDUPE_SECONDS))
    except (ConnectionError, OSError, RuntimeError, ValueError):
        first = True
    if not first:
        return False
    return safely_enqueue_task(fetch_recorded_weather_at, cell[0] / 100, cell[1] / 100, iso_days) is not None


def recorded_weather(location: Location, day: date, *, allow_fetch: bool = True) -> dict[str, Any] | None:
    """The weather on one day at one location, from stored rows or REData.

    Args:
        location: The shared Location the visit's pin points at.
        day: The day to look up.
        allow_fetch: When False, answer only from stored rows.

    Returns:
        That day's record - ``date`` plus REData's fixed-unit ``temperature_max_c``/``temperature_min_c``/``temperature_mean_c``, ``precipitation_mm``, ``snowfall_cm``, ``wind_speed_max_kmh`` and ``wind_gusts_max_kmh`` - or None when the day is outside the record or REData could not answer."""
    if not is_recorded_yet(day):
        return None
    cell = location_cell(location)
    key = day.isoformat()
    record = cached_records({cell: [day]})[cell].get(key)
    if record is None and allow_fetch:
        fetched = _fetch_days(cell[0] / 100, cell[1] / 100, day, day)
        if fetched:
            _store(cell, fetched)
        record = fetched.get(key)
    return record if isinstance(record, dict) else None


def _store(cell: Cell, days: Mapping[str, Any]) -> None:
    """Store fetched days as one row each. A past day never changes, so an existing row is kept.

    Args:
        cell: The cell the days were fetched for.
        days: Mapping of ISO date to record, as returned by REData."""
    from urbanlens.dashboard.models.cache.recorded_weather import RecordedWeatherDay

    rows = []
    for iso, record in days.items():
        try:
            day = date.fromisoformat(iso)
        except ValueError:
            continue
        if isinstance(record, dict):
            rows.append(RecordedWeatherDay(cell_lat=cell[0], cell_lng=cell[1], day=day, data=record))
    if rows:
        RecordedWeatherDay.objects.bulk_create(rows, ignore_conflicts=True)


__all__ = [
    "PUBLICATION_LAG_DAYS",
    "RECORD_BEGINS",
    "Cell",
    "RecordedDay",
    "cached_records",
    "convert_records",
    "is_recorded_yet",
    "location_cell",
    "missing_days",
    "queue_missing_days",
    "recorded_days",
    "recorded_days_at",
    "recorded_weather",
    "to_recorded_day",
    "weather_cell",
]
