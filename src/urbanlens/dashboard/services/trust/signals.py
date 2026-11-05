"""Compute and cache ``ProfileTrustSignals``."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.db.models.functions import GeoHash
from django.db.models import Count, Q
from django.db.models.functions import ExtractIsoYear, ExtractWeek, TruncDate
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

#: Geohash precision of a pin cell for independence (about 1 km); spread is counted on its 5-character prefix (about 5 km).
CELL_PRECISION = 6
SPREAD_PRECISION = 5
MAX_CELLS = 64
ACTIVITY_WINDOW_DAYS = 90
#: Pins newer than this are ignored: a scripted account can scatter fresh pins in minutes.
MIN_PIN_AGE_DAYS = 14

#: A login proves nothing about use, so it does not make a week active.
MEANINGFUL_ACTIVITY_KINDS = tuple(kind for kind in ActivityKind.values if kind != ActivityKind.LOGIN)

#: One client request can post a geolocation visit, and journals, imports and trips are typed in, so these are claims.
CLAIM_VISIT_SOURCES = (VisitSource.GEOLOCATION, VisitSource.MANUAL, VisitSource.USER, VisitSource.HISTORY, VisitSource.TRIP)
#: A photo with GPS and time is harder to fake in bulk, though EXIF is editable, so it counts for more but still little.
PHOTO_VISIT_SOURCES = (VisitSource.PHOTO,)


def _pin_geography(profile: Profile) -> tuple[int, list[str], int]:
    """Return (mature pin count, top recent geohash-6 cells, distinct geohash-5 cells)."""
    now = timezone.now()
    mature = Pin.objects.filter(profile=profile, created__lte=now - timedelta(days=MIN_PIN_AGE_DAYS))
    window_start = now - timedelta(days=settings.UL_TRUST_CELL_WINDOW_DAYS)
    rows = mature.annotate(cell=GeoHash("location__point", precision=CELL_PRECISION)).values("cell").annotate(n=Count("id"), recent=Count("id", filter=Q(created__gte=window_start))).order_by("-recent", "-n", "cell")
    total = 0
    cells: list[str] = []
    spread: set[str] = set()
    for row in rows:
        total += row["n"]
        if not row["cell"]:
            continue
        spread.add(row["cell"][:SPREAD_PRECISION])
        if row["recent"] and len(cells) < MAX_CELLS:
            cells.append(row["cell"])
    return total, cells, len(spread)


def _visit_days(profile: Profile) -> tuple[int, int]:
    """Return (claim, photo) visit days; a day counts once however many pins or requests it spans."""
    day = TruncDate("visited_at")
    counts = PinVisit.objects.filter(pin__profile=profile, tentative=False).aggregate(
        claims=Count(day, distinct=True, filter=Q(source__in=CLAIM_VISIT_SOURCES)),
        photos=Count(day, distinct=True, filter=Q(source__in=PHOTO_VISIT_SOURCES)),
    )
    return counts["claims"], counts["photos"]


def _active_weeks(profile: Profile) -> int:
    since = timezone.localdate() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    weeks = ProfileActivityDay.objects.filter(profile=profile, kind__in=MEANINGFUL_ACTIVITY_KINDS, day__gte=since).annotate(iso_year=ExtractIsoYear("day"), iso_week=ExtractWeek("day")).values("iso_year", "iso_week").distinct()
    return weeks.count()


def _trusted_friend_count(profile: Profile) -> int:
    """
    Existing accepted friends who look like real users. This is passive: no user action, prompt or UI feeds it.

    It counts an accepted friendship at least ``UL_TRUST_FRIENDSHIP_MIN_AGE_DAYS`` old with a profile whose stored ``base_score`` (which excludes this count) cleared the threshold and that met the hard floors.
    """
    cutoff = timezone.now() - timedelta(days=settings.UL_TRUST_FRIENDSHIP_MIN_AGE_DAYS)
    trusted = {
        "base_score__gte": settings.UL_TRUST_REAL_USER_THRESHOLD,
        "account_age_days__gte": settings.UL_TRUST_MIN_AGE_DAYS,
        "active_weeks_90__gte": settings.UL_TRUST_MIN_ACTIVE_WEEKS,
    }
    mine = Q(from_profile=profile, to_profile__trust_signals__in=ProfileTrustSignals.objects.filter(**trusted))
    theirs = Q(to_profile=profile, from_profile__trust_signals__in=ProfileTrustSignals.objects.filter(**trusted))
    return Friendship.objects.filter(status=FriendshipStatus.ACCEPTED, updated__lte=cutoff).filter(mine | theirs).count()


def compute_signals(profile: Profile) -> ProfileTrustSignals:
    """
    Rebuild and store the signals row for ``profile``.

    Returns:
        The saved row. Idempotent: running it again replaces the row's values.
    """
    from urbanlens.dashboard.services.trust.checks import meets_floors, real_user_score

    now = timezone.now()
    pin_count, cells, spread = _pin_geography(profile)
    claims, photos = _visit_days(profile)
    row, _ = ProfileTrustSignals.objects.update_or_create(
        profile=profile,
        defaults={
            "computed_at": now,
            "account_age_days": max((now - profile.created).days, 0),
            "active_weeks_90": _active_weeks(profile),
            "pin_count": pin_count,
            "pin_cells": cells,
            "spread_cells": spread,
            "trusted_friend_count": _trusted_friend_count(profile),
            "claim_visit_days": claims,
            "photo_visit_days": photos,
        },
    )
    base = real_user_score(row, include_trusted_friends=False)
    verdict = meets_floors(row) and real_user_score(row) >= settings.UL_TRUST_REAL_USER_THRESHOLD
    if row.base_score != base or row.is_real_user != verdict:
        row.base_score = base
        row.is_real_user = verdict
        row.save(update_fields=["base_score", "is_real_user", "updated"])
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
