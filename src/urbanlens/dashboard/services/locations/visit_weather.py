"""What the weather actually was on the day of a visit.

REData's ``GET /weather/history/`` answers from Open-Meteo's ERA5 reanalysis -
worldwide, keyless, back to 1940. It is the counterpart of the forecast that
``services.apis.locations.weather_resolution`` serves, and the opposite kind of
fact: a forecast is only meaningful relative to when it was made, while the
record of a day that has already happened never changes.

That immutability is what this module is built around. A recorded day is cached
per :class:`~urbanlens.dashboard.models.location.model.Location` under one
:class:`~urbanlens.dashboard.models.cache.location_cache.LocationCache` row
keyed by ISO date, and a hit is served **without** consulting
``LocationCache.is_stale``: the site-wide external-data freshness window exists
to re-ask sources whose answers drift, and this one cannot.

Two windows have no answer to cache rather than an empty one, so neither is
stored as a negative result:

* Before :data:`RECORD_BEGINS`, because ERA5 does not go back that far.
* Within :data:`PUBLICATION_LAG_DAYS` of today, because ERA5 lags real time -
  those days become answerable later, and caching "nothing" would make them
  permanently blank.

Both are checked locally by :func:`is_recorded_yet` before any request, so the
common case of a visit logged this morning costs no call at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: ``LocationCache.source`` for the per-date record. The row's ``data`` is a
#: mapping of ISO date to that day's record, not a single response body.
CACHE_SOURCE = "redata_weather_history"

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
        False when ``day`` predates :data:`RECORD_BEGINS` or falls inside the
        :data:`PUBLICATION_LAG_DAYS` window - in both cases there is nothing to
        fetch, and the second is temporary.
    """
    current = today or timezone.localdate()
    return RECORD_BEGINS <= day <= current - timedelta(days=PUBLICATION_LAG_DAYS)


def cached_days(location: Location) -> dict[str, Any]:
    """Every recorded day already stored for a location.

    Args:
        location: The shared Location the visit's pin points at.

    Returns:
        A mapping of ISO date to that day's record. Empty when nothing has been
        fetched yet. Deliberately ignores ``LocationCache.is_stale`` - see the
        module docstring.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    entry = LocationCache.objects.filter(location=location, source=CACHE_SOURCE).first()
    if entry is None or not isinstance(entry.data, dict):
        return {}
    return entry.data


@dataclass(slots=True, frozen=True)
class RecordedDay:
    """One recorded day, converted to the units the rest of the app displays.

    REData publishes Celsius, millimetres, centimetres and km/h; every weather
    surface in UrbanLens shows Fahrenheit, inches and mph (see
    ``services.apis.weather.forecast``, which converts the forecast the same
    way). Converting here rather than in the template keeps one implementation
    and one rounding decision.

    Every field is optional because a null from ERA5 is a real answer, not a
    gap: the reanalysis gained some variables later than others, so an early
    year legitimately carries no wind-gust reading.

    Attributes:
        day: The day these readings describe.
        high_f: Daily maximum temperature, degrees Fahrenheit.
        low_f: Daily minimum temperature.
        mean_f: Daily mean temperature.
        precipitation_in: Rainfall as liquid volume, inches.
        snowfall_in: Snow as accumulated depth, inches - not the same quantity
            as ``precipitation_in`` in different units.
        wind_max_mph: Maximum sustained wind.
        gust_max_mph: Maximum gust.
    """

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

        Assembled here rather than in a template because the interesting cases
        are all conditional - a day with a high but no low, a dry day, a
        reading of exactly zero - and each one is a branch a template expresses
        badly and nothing can test.

        Zero is a *reading*: "0.0 in rain" is what a dry day looks like and is
        not worth a clause, so precipitation, snow and gusts appear only when
        non-zero. Temperature is different - a high of 0F is a fact about the
        day - so it is included whenever it is not None.

        Returns:
            Something like ``"72° / 54°F · 0.30 in rain · gusts 31 mph"``, or
            ``""`` when nothing came back.
        """
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
        ``{iso_date: record}`` for the days REData could answer, empty when it
        could not answer at all. A range is one request however wide it is,
        which is what makes the trip surface affordable: a week-long trip costs
        one call per location rather than one per day.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured

    if not redata_configured():
        return {}

    from urbanlens.dashboard.services.apis.locations.redata_weather_gateway import RedataWeatherHistoryGateway

    try:
        days = RedataWeatherHistoryGateway().get_history(latitude, longitude, start=start, end=end)
    except LocationContextUnavailableError:
        logger.info("Historical weather unavailable for %.4f,%.4f %s..%s", latitude, longitude, start, end, exc_info=True)
        return {}
    return {str(entry["date"]): entry for entry in days if isinstance(entry, dict) and entry.get("date")}


def recorded_range(location: Location, start: date, end: date) -> dict[str, RecordedDay]:
    """Every recorded day in a range at a location, cache-first.

    One REData request covers the whole range, so a caller with several days at
    one place (a trip's activities, a run of visits) should use this rather than
    calling :func:`recorded_weather` per day.

    Args:
        location: The shared Location whose coordinates are queried and whose
            cache row the days are stored in.
        start: First day, inclusive.
        end: Last day, inclusive.

    Returns:
        ``{iso_date: RecordedDay}`` for the days that could be answered. Days
        outside the recorded window (see :func:`is_recorded_yet`) are absent
        rather than fetched.
    """
    wanted = [day for day in _days_between(start, end) if is_recorded_yet(day)]
    if not wanted:
        return {}

    cached = cached_days(location)
    missing = [day for day in wanted if day.isoformat() not in cached]
    if missing:
        fetched = _fetch_days(float(location.latitude), float(location.longitude), min(missing), max(missing))
        if fetched:
            _store(location, fetched)
            cached = {**cached, **fetched}

    converted: dict[str, RecordedDay] = {}
    for day in wanted:
        record = cached.get(day.isoformat())
        if isinstance(record, dict) and (recorded := to_recorded_day(record)) is not None:
            converted[day.isoformat()] = recorded
    return converted


#: How far apart two missing days can be and still be fetched as one range.
#: A range is one request however wide it is, so merging is nearly free - up to
#: the point where the answer itself is not. A month of daily records is a small
#: response and a small cache row; the twenty years between two visits to the
#: same ruin is neither, and every day in between would be stored to serve two.
_MERGE_GAP_DAYS = 31


def _clusters(days: list[date]) -> list[tuple[date, date]]:
    """Group sorted days into ranges worth fetching in one request.

    Args:
        days: The days to fetch, in any order.

    Returns:
        ``(start, end)`` pairs covering every day, splitting wherever the gap
        exceeds :data:`_MERGE_GAP_DAYS`.
    """
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


def recorded_days(location: Location, days: Iterable[date], *, allow_fetch: bool = True) -> dict[str, RecordedDay]:
    """Recorded weather for a set of days at one location, cache-first.

    The sparse counterpart of :func:`recorded_range`. That one exists for days
    that are genuinely a range - a trip's activities - and fetches
    ``min..max`` in a single request, which is right there and wrong here: a
    page of visits to the same place can span decades, and asking for every day
    between the first and the last would return (and cache) thousands of days to
    show ten.

    Args:
        location: The shared Location whose coordinates are queried and whose
            cache row the days are stored in.
        days: The days wanted, in any order. Duplicates and days outside the
            recorded window are dropped.
        allow_fetch: When False, answer only from cache. Use this on any path
            that must not make an outbound call - a page render, a list, an
            export. :func:`missing_days` says what such a caller would need to
            queue.

    Returns:
        ``{iso_date: RecordedDay}`` for the days that could be answered.
    """
    wanted = [day for day in set(days) if is_recorded_yet(day)]
    if not wanted:
        return {}

    cached = cached_days(location)
    missing = [] if not allow_fetch else [day for day in wanted if day.isoformat() not in cached]
    for start, end in _clusters(missing):
        fetched = _fetch_days(float(location.latitude), float(location.longitude), start, end)
        if fetched:
            _store(location, fetched)
            cached = {**cached, **fetched}

    converted: dict[str, RecordedDay] = {}
    for day in wanted:
        record = cached.get(day.isoformat())
        if isinstance(record, dict) and (recorded := to_recorded_day(record)) is not None:
            converted[day.isoformat()] = recorded
    return converted


def missing_days(location: Location, days: Iterable[date]) -> list[date]:
    """Which of ``days`` are recordable, wanted, and not cached yet.

    For a caller that reads with ``allow_fetch=False`` and wants the gap filled
    behind it. Days outside ERA5's window are absent rather than listed: they
    are not missing, they are unanswerable, and queueing them would retry
    forever.

    Args:
        location: The Location whose cache row is consulted.
        days: The days wanted, in any order.

    Returns:
        The days worth fetching, sorted.
    """
    cached = cached_days(location)
    return sorted({day for day in days if is_recorded_yet(day) and day.isoformat() not in cached})


def recorded_range_at(latitude: float, longitude: float, start: date, end: date) -> dict[str, RecordedDay]:
    """:func:`recorded_range` for a bare coordinate, with no local cache.

    For a caller that has coordinates but no ``Location`` to key a cache row on -
    a trip activity whose position comes from its own lat/lng override, say.
    REData caches the days on its side regardless, so the cost of the miss is a
    round trip rather than a re-fetch of the underlying source.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        start: First day, inclusive.
        end: Last day, inclusive.

    Returns:
        ``{iso_date: RecordedDay}`` for the days that could be answered.
    """
    wanted = [day for day in _days_between(start, end) if is_recorded_yet(day)]
    if not wanted:
        return {}
    fetched = _fetch_days(latitude, longitude, min(wanted), max(wanted))
    converted: dict[str, RecordedDay] = {}
    for iso, record in fetched.items():
        if (recorded := to_recorded_day(record)) is not None:
            converted[iso] = recorded
    return converted


def _days_between(start: date, end: date) -> list[date]:
    """Every day from ``start`` to ``end`` inclusive, or empty when reversed."""
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def recorded_weather(location: Location, day: date, *, allow_fetch: bool = True) -> dict[str, Any] | None:
    """The weather on one day at one location, from cache or REData.

    Args:
        location: The shared Location the visit's pin points at. Its
            coordinates are what gets queried.
        day: The day to look up.
        allow_fetch: When False, answer only from cache. Use this on any path
            that must not make an outbound call (a list render, a bulk export).

    Returns:
        That day's record - ``date`` plus REData's fixed-unit
        ``temperature_max_c``/``temperature_min_c``/``temperature_mean_c``,
        ``precipitation_mm``, ``snowfall_cm``, ``wind_speed_max_kmh`` and
        ``wind_gusts_max_kmh`` - or None when the day is outside the recorded
        window, the location has no coordinates, REData is not configured, or
        the lookup failed. A null *inside* a returned record is a real answer:
        ERA5 gained some variables later than others.
    """
    if not is_recorded_yet(day):
        return None

    key = day.isoformat()
    cached = cached_days(location)
    if key in cached:
        record = cached[key]
        return record if isinstance(record, dict) else None
    if not allow_fetch:
        return None

    fetched = _fetch_days(float(location.latitude), float(location.longitude), day, day)
    if not fetched:
        # Nothing to store: an empty answer inside the recorded window is a
        # source gap, and caching it would make the day permanently blank.
        return None

    _store(location, fetched)
    record = fetched.get(key)
    return record if isinstance(record, dict) else None


def _store(location: Location, days: dict[str, Any]) -> None:
    """Merge fetched days into the location's cached record.

    Merges rather than replaces so a lookup for one date keeps the days another
    already stored, and re-reads the row under a row lock so two concurrent
    lookups for different dates cannot each write a copy missing the other's.

    Args:
        location: The Location to cache against.
        days: Mapping of ISO date to record, as returned by REData.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    # get_or_create first so the lock below has a row to take; the unique
    # constraint on (location, source) is what makes the racing caller wait
    # rather than insert a second one.
    LocationCache.objects.get_or_create(location=location, source=CACHE_SOURCE, defaults={"data": {}})
    with transaction.atomic():
        entry = LocationCache.objects.select_for_update().get(location=location, source=CACHE_SOURCE)
        merged = dict(entry.data) if isinstance(entry.data, dict) else {}
        merged.update(days)
        entry.data = merged
        entry.save(update_fields=["data", "updated"])


__all__ = [
    "CACHE_SOURCE",
    "PUBLICATION_LAG_DAYS",
    "RECORD_BEGINS",
    "RecordedDay",
    "cached_days",
    "is_recorded_yet",
    "recorded_range",
    "recorded_range_at",
    "recorded_weather",
    "to_recorded_day",
]
