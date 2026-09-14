"""Shared photo-import mode constants and pin-visit-date lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import TextChoices

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.pin.model import Pin

# Bounds how many single-day date-range API calls a "during my visits" search
# makes against a provider - one call per distinct visit date.
MAX_VISIT_DATES = 15


class PhotoImportMode(TextChoices):
    """Which subset of a connected photo library a pin-detail search covers."""

    NEARBY = "nearby", "Nearby"
    VISITS = "visits", "During My Visits"
    ALL = "all", "All Photos"


def visit_dates_for_pin(pin: Pin, limit: int = MAX_VISIT_DATES) -> list[datetime.date]:
    """Return a pin's most recent distinct PinVisit calendar dates.

    Args:
        pin: The pin whose visit history to read.
        limit: Maximum number of distinct dates to return, most recent first.

    Returns:
        Distinct visit dates, newest first."""
    dates: list[datetime.date] = []
    for visited_at in pin.visit_history.order_by("-visited_at").values_list("visited_at", flat=True):
        day = visited_at.date()
        if day not in dates:
            dates.append(day)
            if len(dates) >= limit:
                break
    return dates
