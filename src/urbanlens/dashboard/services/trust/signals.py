"""Compute and cache ``ProfileTrustSignals``."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.db.models.functions import GeoHash
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from urbanlens.dashboard.models.achievements.model import ProfileActivityDay
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Geohash precision of a pin cell (about 40 km).
CELL_PRECISION = 4
MAX_CELLS = 64
ACTIVITY_WINDOW_DAYS = 90

#: Strongest evidence first. A visit counts once per pin per day however it was logged.
DEVICE_TRACK_SOURCES = frozenset({VisitSource.GEOLOCATION})
PHOTO_SOURCES = frozenset({VisitSource.PHOTO})
#: Everything else a pin owner asserts. ``USER`` is a friend's journal, not this profile's own evidence.
RECORDED_SOURCES = frozenset({VisitSource.MANUAL, VisitSource.HISTORY, VisitSource.TRIP, VisitSource.SAFETY_CHECKIN})


def _pin_cells(profile: Profile) -> tuple[int, list[str]]:
    rows = Pin.objects.filter(profile=profile).annotate(cell=GeoHash("location__point", precision=CELL_PRECISION)).values("cell").annotate(n=Count("id")).order_by("-n", "cell")
    total = 0
    cells: list[str] = []
    for row in rows:
        total += row["n"]
        if row["cell"] and len(cells) < MAX_CELLS:
            cells.append(row["cell"])
    return total, cells


def _visit_counts(profile: Profile) -> dict[str, int]:
    rows = PinVisit.objects.filter(pin__profile=profile, tentative=False).annotate(day=TruncDate("visited_at")).values_list("source", "pin_id", "day").distinct()
    counts = {"device": 0, "photo": 0, "recorded": 0}
    for source, _pin_id, _day in rows:
        if source in DEVICE_TRACK_SOURCES:
            counts["device"] += 1
        elif source in PHOTO_SOURCES:
            counts["photo"] += 1
        elif source in RECORDED_SOURCES:
            counts["recorded"] += 1
    return counts


def compute_signals(profile: Profile) -> ProfileTrustSignals:
    """
    Rebuild and store the signals row for ``profile``.

    Returns:
        The saved row. Idempotent: running it again replaces the row's values.
    """
    now = timezone.now()
    pin_count, cells = _pin_cells(profile)
    visits = _visit_counts(profile)
    active_days = ProfileActivityDay.objects.filter(profile=profile, day__gte=timezone.localdate() - timedelta(days=ACTIVITY_WINDOW_DAYS)).values("day").distinct().count()
    row, _ = ProfileTrustSignals.objects.update_or_create(
        profile=profile,
        defaults={
            "computed_at": now,
            "account_age_days": max((now - profile.created).days, 0),
            "active_days_90": active_days,
            "pin_count": pin_count,
            "pin_cells": cells,
            "device_track_visits": visits["device"],
            "photo_visits": visits["photo"],
            "recorded_visits": visits["recorded"],
        },
    )
    return row


def signals_for(profile: Profile) -> ProfileTrustSignals:
    """
    Return the profile's signals, recomputing when the row is missing or stale.

    The common case is one indexed read.
    """
    row = ProfileTrustSignals.objects.filter(profile=profile).first()
    max_age = timedelta(seconds=settings.UL_TRUST_SIGNALS_MAX_AGE_SECONDS)
    if row is None or timezone.now() - row.computed_at > max_age:
        return compute_signals(profile)
    return row
