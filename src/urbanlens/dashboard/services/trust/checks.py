"""Real-user score and pairwise independence, from ``ProfileTrustSignals`` alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q

from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.services.trust.signals import signals_for

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals

#: Each component saturates, so no single cheap signal can carry the score.
_AGE_FULL_DAYS = 180
_ACTIVE_DAYS_FULL = 12
_PINS_FULL = 10
_SPREAD_FULL_REGIONS = 2
_VOUCHES_FULL = 3
#: Visit evidence weights, strongest first. A recorded visit is a claim, so its total is capped.
_DEVICE_WEIGHT = 3.0
_PHOTO_WEIGHT = 2.0
_RECORDED_WEIGHT = 0.5
_RECORDED_CAP = 1.0
_VISITS_FULL = 6.0
#: Component weights: age, meaningful active days, mature pins, regional spread, verified visits, vouches.
#: Age plus steady use plus a real pin history can pass without any visit; visits and vouches alone cannot rescue a fresh account.
_WEIGHTS = (2.0, 2.0, 1.0, 1.0, 2.0, 1.0)


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
        min(signals.region_count / _SPREAD_FULL_REGIONS, 1.0),
        min(visit_points / _VISITS_FULL, 1.0),
        min(signals.vouch_count / _VOUCHES_FULL, 1.0),
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


def _creation_ok(a: Profile, b: Profile, sig_a: ProfileTrustSignals, sig_b: ProfileTrustSignals) -> bool:
    gap = abs((a.created - b.created).days)
    both_old = min(sig_a.account_age_days, sig_b.account_age_days) > settings.UL_TRUST_BOTH_ESTABLISHED_DAYS
    return gap > settings.UL_TRUST_MIN_CREATION_GAP_DAYS or both_old


def _same_invitation_chain(a: Profile, b: Profile) -> bool:
    """Whether one invited the other, or one inviter brought in both."""
    rows = list(FriendInvitation.objects.filter(invitee__in=(a, b), accepted_at__isnull=False).values_list("invitee_id", "inviter_id"))
    if any(inviter_id in (a.pk, b.pk) for _, inviter_id in rows):
        return True
    return len({inviter_id for _, inviter_id in rows}) < len({invitee_id for invitee_id, _ in rows})


def are_independent(a: Profile, b: Profile, *, exclude_cell: str | None = None) -> bool:
    """
    Whether two profiles look like unrelated people rather than one person's accounts.

    The pair must not have been registered within ``UL_TRUST_MIN_CREATION_GAP_DAYS`` of each other unless both are older than ``UL_TRUST_BOTH_ESTABLISHED_DAYS``, and must not share an invitation chain.
    Then the Jaccard overlap of their geohash-6 pin cells, ignoring ``exclude_cell`` (the cell of the place being decided, at the same precision, which they share by construction), must be below ``UL_TRUST_MAX_CELL_JACCARD``; co-located real users pass.
    A profile too small to have usable geography must instead reach ``UL_TRUST_NO_GEOGRAPHY_THRESHOLD`` as a real user.
    An accepted friendship only raises the allowed overlap, so it can turn a fail into a pass and never the reverse.
    Run it from a Celery task: it reads two signal rows and, for a passing pair, the invitation table.
    """
    if a.pk == b.pk:
        return False
    sig_a, sig_b = signals_for(a), signals_for(b)
    if not _creation_ok(a, b, sig_a, sig_b):
        return False
    if _has_geography(sig_a) and _has_geography(sig_b):
        overlap = _jaccard(set(sig_a.pin_cells) - {exclude_cell}, set(sig_b.pin_cells) - {exclude_cell})
        if overlap is None:
            return False
        limit = settings.UL_TRUST_MAX_CELL_JACCARD
        if overlap >= limit and not (overlap < limit + settings.UL_TRUST_FRIENDSHIP_JACCARD_BONUS and _accepted_friends(a, b)):
            return False
    elif not all(real_user_score(sig) >= settings.UL_TRUST_NO_GEOGRAPHY_THRESHOLD for sig in (sig_a, sig_b) if not _has_geography(sig)):
        return False
    return not _same_invitation_chain(a, b)
