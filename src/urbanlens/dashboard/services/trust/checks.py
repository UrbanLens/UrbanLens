"""Real-user score and pairwise independence, from ``ProfileTrustSignals`` alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.services.trust.signals import signals_for

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals

#: Each component saturates, so no single cheap signal can carry the score.
_AGE_FULL_DAYS = 180
_ACTIVE_DAYS_FULL = 20
_PINS_FULL = 10
#: Visit evidence weights, strongest first. A recorded visit is a claim, so its total is capped.
_DEVICE_WEIGHT = 3.0
_PHOTO_WEIGHT = 2.0
_RECORDED_WEIGHT = 0.5
_RECORDED_CAP = 1.0
_VISITS_FULL = 6.0
#: Component weights: age, active days, pins, verified visits.
_WEIGHTS = (1.0, 2.0, 1.0, 2.0)


def real_user_score(signals: ProfileTrustSignals) -> float:
    """
    Weighted 0-1 score of how much the signals look like a real, engaged person.

    Never show it to another user.
    """
    visit_points = min(signals.recorded_visits * _RECORDED_WEIGHT, _RECORDED_CAP) + signals.device_track_visits * _DEVICE_WEIGHT + signals.photo_visits * _PHOTO_WEIGHT
    parts = (
        min(signals.account_age_days / _AGE_FULL_DAYS, 1.0),
        min(signals.active_days_90 / _ACTIVE_DAYS_FULL, 1.0),
        min(signals.pin_count / _PINS_FULL, 1.0),
        min(visit_points / _VISITS_FULL, 1.0),
    )
    return sum(w * p for w, p in zip(_WEIGHTS, parts, strict=True)) / sum(_WEIGHTS)


def is_real_user(profile: Profile) -> bool:
    """Whether the profile's score reaches ``UL_TRUST_REAL_USER_THRESHOLD``."""
    return real_user_score(signals_for(profile)) >= settings.UL_TRUST_REAL_USER_THRESHOLD


def _has_geography(signals: ProfileTrustSignals) -> bool:
    return signals.pin_count >= settings.UL_TRUST_MIN_PINS_FOR_GEOGRAPHY


def _jaccard(a: set[str], b: set[str]) -> float | None:
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _accepted_friends(a: Profile, b: Profile) -> bool:
    return Friendship.objects.filter(status=FriendshipStatus.ACCEPTED).filter(Q(from_profile=a, to_profile=b) | Q(from_profile=b, to_profile=a)).exists()


def are_independent(a: Profile, b: Profile, *, exclude_cell: str | None = None) -> bool:
    """
    Whether two profiles look like unrelated people rather than one person's accounts.

    Compares the Jaccard overlap of their pin cells, ignoring ``exclude_cell`` (the cell of the place being decided, which they share by construction).
    A profile too small to have usable geography must instead reach ``UL_TRUST_NO_GEOGRAPHY_THRESHOLD`` as a real user.
    An accepted friendship only raises the allowed overlap, so it can turn a fail into a pass and never the reverse.
    """
    if a.pk == b.pk:
        return False
    sig_a, sig_b = signals_for(a), signals_for(b)
    if not (_has_geography(sig_a) and _has_geography(sig_b)):
        return all(real_user_score(sig) >= settings.UL_TRUST_NO_GEOGRAPHY_THRESHOLD for sig in (sig_a, sig_b) if not _has_geography(sig))
    cells_a = set(sig_a.pin_cells) - {exclude_cell}
    cells_b = set(sig_b.pin_cells) - {exclude_cell}
    overlap = _jaccard(cells_a, cells_b)
    if overlap is None:
        return False
    if overlap < settings.UL_TRUST_MAX_CELL_JACCARD:
        return True
    return overlap < settings.UL_TRUST_MAX_CELL_JACCARD + settings.UL_TRUST_FRIENDSHIP_JACCARD_BONUS and _accepted_friends(a, b)
