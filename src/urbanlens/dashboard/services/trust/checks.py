"""Real-user score and pairwise independence, from ``ProfileTrustSignals`` alone."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.services.trust.signals import signals_for

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals

#: Each component saturates, so no single cheap signal can carry the score.
_AGE_FULL_DAYS = 180
_ACTIVE_WEEKS_FULL = 6
_PINS_FULL = 10
_SPREAD_FULL_CELLS = 3
_TRUSTED_FRIENDS_FULL = 3
#: Visit days are claims (0.25 each) or photo evidence (0.5 each); the component fills at 1.0, so no source fills it in one request.
_CLAIM_VISIT_VALUE = 0.25
_PHOTO_VISIT_VALUE = 0.5
#: Component weights (total 9): age, active weeks, mature pins, spread, visits, trusted friends.
#: Calendar time dominates; visits and trusted friends are minor and cannot rescue a young or dormant account.
_WEIGHTS = (2.0, 3.0, 1.0, 1.0, 1.0, 1.0)


def meets_floors(signals: ProfileTrustSignals) -> bool:
    """The hard minimums a real user needs whatever the score: account age and weeks of meaningful activity."""
    return signals.account_age_days >= settings.UL_TRUST_MIN_AGE_DAYS and signals.active_weeks_90 >= settings.UL_TRUST_MIN_ACTIVE_WEEKS


def real_user_score(signals: ProfileTrustSignals, *, include_trusted_friends: bool = True) -> float:
    """
    Weighted 0-1 score of how much the signals look like a real, engaged person.

    Never show it to another user. It ignores the hard floors; :func:`is_real_user` applies them.
    Age and steady use over calendar weeks, a mature spread-out pin history and a couple of visits qualify a profile; trusted friends are an optional, passive part and excluded from ``ProfileTrustSignals.base_score``.
    """
    visit_value = signals.claim_visit_days * _CLAIM_VISIT_VALUE + signals.photo_visit_days * _PHOTO_VISIT_VALUE
    parts = (
        min(signals.account_age_days / _AGE_FULL_DAYS, 1.0),
        min(signals.active_weeks_90 / _ACTIVE_WEEKS_FULL, 1.0),
        min(signals.pin_count / _PINS_FULL, 1.0),
        min(signals.spread_cells / _SPREAD_FULL_CELLS, 1.0),
        min(visit_value, 1.0),
        min(signals.trusted_friend_count / _TRUSTED_FRIENDS_FULL, 1.0) if include_trusted_friends else 0.0,
    )
    return sum(w * p for w, p in zip(_WEIGHTS, parts, strict=True)) / sum(_WEIGHTS)


def is_real_user(profile: Profile) -> bool:
    """Whether the profile meets the hard floors and its score reaches ``UL_TRUST_REAL_USER_THRESHOLD``."""
    signals = signals_for(profile)
    return meets_floors(signals) and real_user_score(signals) >= settings.UL_TRUST_REAL_USER_THRESHOLD


def _cells_without(signals: ProfileTrustSignals, exclude_cell: str | None) -> set[str]:
    return set(signals.pin_cells) - {exclude_cell}


def _has_geography(signals: ProfileTrustSignals, exclude_cell: str | None = None) -> bool:
    """Enough mature pins, and enough cells in the window once the decided place's cell is removed (pins older than the window leave no cells)."""
    return signals.pin_count >= settings.UL_TRUST_MIN_PINS_FOR_GEOGRAPHY and len(_cells_without(signals, exclude_cell)) >= settings.UL_TRUST_MIN_CELLS_FOR_GEOGRAPHY


def _jaccard(a: set[str], b: set[str]) -> float | None:
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _established_friends(a: Profile, b: Profile) -> bool:
    cutoff = timezone.now() - timedelta(days=settings.UL_TRUST_FRIENDSHIP_MIN_AGE_DAYS)
    pair = Q(from_profile=a, to_profile=b) | Q(from_profile=b, to_profile=a)
    return Friendship.objects.filter(status=FriendshipStatus.ACCEPTED, updated__lte=cutoff).filter(pair).exists()


def _both_established(sig_a: ProfileTrustSignals, sig_b: ProfileTrustSignals) -> bool:
    return min(sig_a.account_age_days, sig_b.account_age_days) > settings.UL_TRUST_BOTH_ESTABLISHED_DAYS


def _creation_ok(a: Profile, b: Profile, sig_a: ProfileTrustSignals, sig_b: ProfileTrustSignals) -> bool:
    gap = abs((a.created - b.created).days)
    return gap > settings.UL_TRUST_MIN_CREATION_GAP_DAYS or _both_established(sig_a, sig_b)


def _invitation_linked(a: Profile, b: Profile) -> bool:
    """Whether one accepted the other's invitation, or both accepted invitations from the same inviter."""
    inviters: dict[int, set[int]] = {a.pk: set(), b.pk: set()}
    for invitee_id, inviter_id in FriendInvitation.objects.filter(invitee__in=(a, b), accepted_at__isnull=False).values_list("invitee_id", "inviter_id"):
        inviters[invitee_id].add(inviter_id)
    return b.pk in inviters[a.pk] or a.pk in inviters[b.pk] or bool(inviters[a.pk] & inviters[b.pk])


def are_independent(a: Profile, b: Profile, *, exclude_cell: str | None = None) -> bool:
    """
    Whether two profiles look like unrelated people rather than one person's accounts.

    Unless both accounts are older than ``UL_TRUST_BOTH_ESTABLISHED_DAYS``, the pair must have been created more than ``UL_TRUST_MIN_CREATION_GAP_DAYS`` apart and must not be invitation-linked (one invited the other, or one inviter brought in both).
    Then the Jaccard overlap of their geohash-6 pin cells (pins from the last ``UL_TRUST_CELL_WINDOW_DAYS`` days), ignoring ``exclude_cell`` (the cell of the place being decided, at the same precision, which they share by construction), must be below ``UL_TRUST_MAX_CELL_JACCARD``; co-located real users pass.
    An accepted friendship at least ``UL_TRUST_FRIENDSHIP_MIN_AGE_DAYS`` old raises that limit to ``UL_TRUST_FRIENDSHIP_JACCARD_LIMIT`` and never causes a fail.
    A profile with fewer than ``UL_TRUST_MIN_CELLS_FOR_GEOGRAPHY`` window cells left after removing ``exclude_cell`` (or too few pins) has no usable geography and is never treated as zero overlap; it must instead reach ``UL_TRUST_NO_GEOGRAPHY_THRESHOLD`` as a real user.
    Run it from a Celery task: it reads two signal rows and, as needed, the friendship and invitation tables.
    """
    if a.pk == b.pk:
        return False
    sig_a, sig_b = signals_for(a), signals_for(b)
    established = _both_established(sig_a, sig_b)
    if not _creation_ok(a, b, sig_a, sig_b):
        return False
    if _has_geography(sig_a, exclude_cell) and _has_geography(sig_b, exclude_cell):
        overlap = _jaccard(_cells_without(sig_a, exclude_cell), _cells_without(sig_b, exclude_cell))
        if overlap is None:
            return False
        if overlap >= settings.UL_TRUST_MAX_CELL_JACCARD and not (overlap < settings.UL_TRUST_FRIENDSHIP_JACCARD_LIMIT and _established_friends(a, b)):
            return False
    elif not all(real_user_score(sig) >= settings.UL_TRUST_NO_GEOGRAPHY_THRESHOLD and meets_floors(sig) for sig in (sig_a, sig_b) if not _has_geography(sig, exclude_cell)):
        return False
    return established or not _invitation_linked(a, b)
