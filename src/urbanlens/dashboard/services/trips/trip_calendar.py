"""One month of a profile's trips, laid out as a calendar grid on the server."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
import datetime
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Trip chips drawn in one day cell before the rest collapse into "+N more".
CHIPS_PER_DAY = 3

WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


@dataclass(frozen=True, slots=True)
class CalendarTrip:
    """One trip as a day cell draws it."""

    name: str
    status: str
    url: str
    start: datetime.date
    end: datetime.date


@dataclass(slots=True)
class CalendarDay:
    """One cell of the grid; ``day`` is None for the padding before the 1st."""

    day: datetime.date | None
    is_today: bool = False
    trips: list[CalendarTrip] = field(default_factory=list)
    more: int = 0


@dataclass(frozen=True, slots=True)
class TripMonth:
    """A month's grid plus what the prev/next controls need."""

    first: datetime.date
    days: list[CalendarDay]
    previous: str
    next: str

    @property
    def weekdays(self) -> tuple[str, ...]:
        """Column headings, Sunday first to match the grid."""
        return WEEKDAYS


def month_param(value: datetime.date) -> str:
    """The ``?month=`` value naming *value*'s month."""
    return value.strftime("%Y-%m")


def parse_month(raw: str | None, today: datetime.date) -> datetime.date:
    """The first day of the month *raw* names (``YYYY-MM``), or of *today*'s month when it names none.

    Args:
        raw: The ``month`` query parameter, possibly absent or malformed.
        today: The fallback.

    Returns:
        A first-of-month date.
    """
    try:
        parsed = datetime.datetime.strptime(raw or "", "%Y-%m").date()  # noqa: DTZ007 - a calendar month, not an instant
    except ValueError:
        parsed = today
    return parsed.replace(day=1)


def trip_month(profile: Profile, month: datetime.date) -> TripMonth:
    """Lay out *profile*'s trips that meet *month* on a Sunday-first grid.

    One query reads just the trips overlapping the month, whatever the account's history.

    Args:
        profile: Whose trips.
        month: Any day in the month to show.

    Returns:
        The month's grid.
    """
    from urbanlens.dashboard.models.trips.model import Trip

    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    today = timezone.now().date()
    rows = Trip.objects.filter(pk__in=Trip.objects.filter(profiles=profile).values("pk")).overlapping(first, last).with_timeline_status().order_by("_eff_start", "pk").values_list("name", "slug", "timeline", "_eff_start", "_eff_end")
    trips = [CalendarTrip(name=name, status=status, url=reverse("trips.detail", args=[slug]), start=start, end=end or start) for name, slug, status, start, end in rows]

    leading = (first.weekday() + 1) % 7
    days = [CalendarDay(day=None) for _ in range(leading)]
    for offset in range(last.day):
        day = first + datetime.timedelta(days=offset)
        cell = CalendarDay(day=day, is_today=day == today)
        on_day = [trip for trip in trips if trip.start <= day <= trip.end]
        cell.trips = on_day[:CHIPS_PER_DAY]
        cell.more = max(0, len(on_day) - CHIPS_PER_DAY)
        days.append(cell)

    previous = (first - datetime.timedelta(days=1)).replace(day=1)
    following = last + datetime.timedelta(days=1)
    return TripMonth(first=first, days=days, previous=month_param(previous), next=month_param(following))
