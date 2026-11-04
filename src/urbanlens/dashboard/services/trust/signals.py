"""Compute and cache ``ProfileTrustSignals``."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.db.models.functions import GeoHash
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from urbanlens.dashboard.models.achievements.meta import ActivityKind
from urbanlens.dashboard.models.achievements.model import ProfileActivityDay
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Geohash precision of a pin cell for independence (about 1 km) and of a region for spread (about 40 km).
CELL_PRECISION = 6
REGION_PRECISION = 4
MAX_CELLS = 64
ACTIVITY_WINDOW_DAYS = 90
#: Pins newer than this are ignored: a scripted account can scatter fresh pins in minutes.
MIN_PIN_AGE_DAYS = 14

#: A login proves nothing about use, so it does not make a day active.
MEANINGFUL_ACTIVITY_KINDS = tuple(kind for kind in ActivityKind.values if kind != ActivityKind.LOGIN)

#: Strongest evidence first. A visit counts once per pin per day however it was logged.
DEVICE_TRACK_SOURCES = frozenset({VisitSource.GEOLOCATION})
PHOTO_SOURCES = frozenset({VisitSource.PHOTO})
#: Everything else a pin owner asserts. ``USER`` is a friend's journal, not this profile's own evidence.
RECORDED_SOURCES = frozenset({VisitSource.MANUAL, VisitSource.HISTORY, VisitSource.TRIP, VisitSource.SAFETY_CHECKIN})


def _mature_pins(profile: Profile):
    return Pin.objects.filter(profile=profile, created__lte=timezone.now() - timedelta(days=MIN_PIN_AGE_DAYS))


def _pin_geography(profile: Profile) -> tuple[int, list[str], int]:
    """Return (mature pin count, top geohash-6 cells, distinct geohash-4 regions)."""
    pins = _mature_pins(profile)
    rows = pins.annotate(cell=GeoHash("location__point", precision=CELL_PRECISION)).values("cell").annotate(n=Count("id")).order_by("-n", "cell")
    total = 0
    cells: list[str] = []
    regions: set[str] = set()
    for row in rows:
        total += row["n"]
        if row["cell"]:
            regions.add(row["cell"][:REGION_PRECISION])
            if len(cells) < MAX_CELLS:
                cells.append(row["cell"])
    return total, cells, len(regions)


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


def _vouch_count(profile: Profile) -> int:
    """Accepted friends whose stored verdict is real-user; reads their rows, never recomputes them."""
    return Friendship.objects.filter(status=FriendshipStatus.ACCEPTED).filter(Q(from_profile=profile, to_profile__trust_signals__is_real_user=True) | Q(to_profile=profile, from_profile__trust_signals__is_real_user=True)).count()


def compute_signals(profile: Profile) -> ProfileTrustSignals:
    """
    Rebuild and store the signals row for ``profile``.

    Returns:
        The saved row. Idempotent: running it again replaces the row's values.
    """
    from urbanlens.dashboard.services.trust.checks import real_user_score

    now = timezone.now()
    pin_count, cells, regions = _pin_geography(profile)
    visits = _visit_counts(profile)
    active_days = ProfileActivityDay.objects.filter(profile=profile, kind__in=MEANINGFUL_ACTIVITY_KINDS, day__gte=timezone.localdate() - timedelta(days=ACTIVITY_WINDOW_DAYS)).values("day").distinct().count()
    row, _ = ProfileTrustSignals.objects.update_or_create(
        profile=profile,
        defaults={
            "computed_at": now,
            "account_age_days": max((now - profile.created).days, 0),
            "active_days_90": active_days,
            "pin_count": pin_count,
            "pin_cells": cells,
            "region_count": regions,
            "vouch_count": _vouch_count(profile),
            "device_track_visits": visits["device"],
            "photo_visits": visits["photo"],
            "recorded_visits": visits["recorded"],
        },
    )
    verdict = real_user_score(row) >= settings.UL_TRUST_REAL_USER_THRESHOLD
    if row.is_real_user != verdict:
        row.is_real_user = verdict
        row.save(update_fields=["is_real_user", "updated"])
    return row


def signals_for(profile: Profile) -> ProfileTrustSignals:
    """
    Return the profile's signals, recomputing when the row is missing or stale.

    The common case is one indexed read. A recompute is several queries, so call this from Celery tasks, not request handlers.
    """
    row = ProfileTrustSignals.objects.filter(profile=profile).first()
    max_age = timedelta(seconds=settings.UL_TRUST_SIGNALS_MAX_AGE_SECONDS)
    if row is None or timezone.now() - row.computed_at > max_age:
        return compute_signals(profile)
    return row
