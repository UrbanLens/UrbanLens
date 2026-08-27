"""Recording daily activity and maintaining streak counters.

Streaks are built from one row per profile per kind per calendar day. Callers
just say "this profile did X" and :func:`record_activity` collapses repeats
within a day, so the fifty photos someone uploads in an afternoon advance the
photo streak by exactly one.
"""

# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# Django Imports
from django.db import transaction
from django.utils import timezone

# App Imports
from urbanlens.dashboard.models.achievements.meta import ActivityKind

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.achievements.model import ProfileStreak
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


def _owner_filter(profile: Profile | int) -> dict[str, Any]:
    """Return the lookup kwargs identifying *profile*, accepting a bare PK.

    Signal handlers only have ``instance.profile_id``, and fetching the whole
    Profile just to write an activity row would add a query to every
    contributing write.
    """
    return {"profile_id": profile} if isinstance(profile, int) else {"profile": profile}


def record_activity(profile: Profile | int, kind: str, day: datetime.date | None = None) -> bool:
    """Record that *profile* performed *kind* on *day*, advancing their streak.

    Safe to call on every single write - the per-day uniqueness constraint makes
    repeats within a day a no-op, and only the first call of the day touches the
    streak counters.

    Args:
        profile: Who acted, as a Profile or its PK.
        kind: An :class:`ActivityKind` value.
        day: Local calendar date of the action; defaults to today. Backfills may
            pass an earlier date, in which case the streak is rebuilt from the
            raw activity rows rather than incremented.

    Returns:
        True when this was the profile's first activity of that kind on that
        day (so a streak may have changed), False when it was a repeat.
    """
    from urbanlens.dashboard.models.achievements.model import ProfileActivityDay

    if kind not in ActivityKind.values:
        logger.warning("Ignoring unknown activity kind %r", kind)
        return False

    day = day or timezone.localdate()
    _, created = ProfileActivityDay.objects.get_or_create(kind=kind, day=day, **_owner_filter(profile))
    if not created:
        return False

    streak = _advance_streak(profile, kind, day)
    logger.debug("Activity %s recorded for profile %s on %s (streak now %s)", kind, profile, day, streak.current_length)
    return True


def _advance_streak(profile: Profile | int, kind: str, day: datetime.date) -> ProfileStreak:
    """Move the cached streak for (*profile*, *kind*) forward to include *day*.

    Out-of-order days (a backfill filling in the past) cannot be handled by
    incrementing, so those fall through to a full rebuild.

    Args:
        profile: Whose streak to advance, as a Profile or its PK.
        kind: The activity kind.
        day: The newly recorded day.

    Returns:
        The updated streak row.
    """
    from urbanlens.dashboard.models.achievements.model import ProfileStreak

    with transaction.atomic():
        streak, _ = ProfileStreak.objects.select_for_update().get_or_create(kind=kind, **_owner_filter(profile))

        if streak.last_day is not None and day <= streak.last_day:
            return rebuild_streak(profile, kind)

        if streak.last_day is not None and (day - streak.last_day).days == 1:
            streak.current_length += 1
        else:
            streak.current_length = 1

        streak.last_day = day
        streak.longest_length = max(streak.longest_length, streak.current_length)
        streak.save(update_fields=["current_length", "longest_length", "last_day", "updated"])
        return streak


def rebuild_streak(profile: Profile | int, kind: str) -> ProfileStreak:
    """Recompute a profile's streak for one kind from the raw activity rows.

    Used when days arrive out of order, and available as a repair tool if the
    cached counters are ever suspected of drifting.

    Args:
        profile: Whose streak to rebuild, as a Profile or its PK.
        kind: The activity kind to rebuild.

    Returns:
        The rebuilt streak row.
    """
    from urbanlens.dashboard.models.achievements.model import ProfileActivityDay, ProfileStreak

    owner = _owner_filter(profile)
    days = list(
        ProfileActivityDay.objects.filter(kind=kind, **owner).order_by("day").values_list("day", flat=True),
    )

    longest = 0
    current = 0
    previous: datetime.date | None = None
    for day in days:
        current = current + 1 if previous is not None and (day - previous).days == 1 else 1
        longest = max(longest, current)
        previous = day

    streak, _ = ProfileStreak.objects.get_or_create(kind=kind, **owner)
    streak.current_length = current
    streak.longest_length = longest
    streak.last_day = previous
    streak.save(update_fields=["current_length", "longest_length", "last_day", "updated"])
    return streak


__all__ = [
    "rebuild_streak",
    "record_activity",
]
