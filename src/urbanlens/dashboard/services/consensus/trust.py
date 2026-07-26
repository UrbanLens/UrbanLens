"""Trust scoring for Consensus - Beta-Bernoulli posterior with exponential forgetting.

A rare "trust-check" round shows a player data that's actually already
known/confirmed, disguised as an ordinary round (see
``services.consensus.selection``), to verify they're answering accurately.
Trust adapts over time rather than accumulating into a nearly-immovable
lifetime average - a decay factor is applied to the posterior before each
new observation, so a recent run of wrong answers visibly drags a
previously-trusted profile's score down (the standard "Beta reputation
system" forgetting-factor technique - see Jøsang & Ismail, 2002).
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.consensus.model import ConsensusProfile
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Decay applied to the Beta posterior (relative to the prior) before each
#: new check-result update - an event ~50 checks ago retains ~36% weight,
#: ~100 checks ago ~13%, so a recent bad streak dominates within a normal
#: play session instead of being lost in a lifetime average.
TRUST_DECAY_GAMMA = 0.98

#: Injection-probability floor/ceiling. Even a maximally trusted veteran is
#: occasionally re-checked (the floor); even a brand-new/untrusted player
#: isn't checked on much more than a third of their rounds (the ceiling) -
#: "periodically re-confirm, but don't overwhelm well-intentioned players."
CHECK_PROBABILITY_MIN = 0.02
CHECK_PROBABILITY_MAX = 0.35

#: Minimum time between two check rounds for the same profile, regardless of
#: how low their trust score is - so a low-trust player is never checked on
#: back-to-back rounds.
MIN_CHECK_COOLDOWN_SECONDS = 120


def trust_score(consensus_profile: ConsensusProfile) -> float:
    """Posterior mean accuracy on trust-check rounds, in (0, 1)."""
    return consensus_profile.trust_score


def trust_score_for_profile(profile: Profile) -> float:
    """``trust_score``, for a Profile - creates its ``ConsensusProfile`` row if missing."""
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    return trust_score(ConsensusProfile.objects.get_or_create_for(profile))


def check_probability(score: float) -> float:
    """Injection probability for a trust score - linear in ``1 - score``, floored and ceilinged."""
    clamped_score = max(0.0, min(1.0, score))
    raw = CHECK_PROBABILITY_MIN + (CHECK_PROBABILITY_MAX - CHECK_PROBABILITY_MIN) * (1.0 - clamped_score)
    return max(CHECK_PROBABILITY_MIN, min(CHECK_PROBABILITY_MAX, raw))


def _on_cooldown(consensus_profile: ConsensusProfile) -> bool:
    if consensus_profile.last_check_at is None:
        return False
    elapsed = (timezone.now() - consensus_profile.last_check_at).total_seconds()
    return elapsed < MIN_CHECK_COOLDOWN_SECONDS


def should_inject_check(profile: Profile) -> bool:
    """Whether ``profile``'s next solo round should be a trust-check round."""
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    consensus_profile = ConsensusProfile.objects.get_or_create_for(profile)
    if _on_cooldown(consensus_profile):
        return False
    probability = check_probability(trust_score(consensus_profile))
    return random.random() < probability  # noqa: S311 - not a cryptographic/security use


def should_inject_check_for_profiles(profiles: Iterable[Profile]) -> bool:
    """Competitive-mode variant: whether the next round for every joined participant should be a check round.

    A competitive round's content is identical for every participant, so
    this uses the *minimum* trust score across the roster (the least-
    trusted participant drives the decision) and honors every participant's
    individual cooldown - a check round is skipped if any one of them was
    just checked.
    """
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    consensus_profiles = [ConsensusProfile.objects.get_or_create_for(profile) for profile in profiles]
    if not consensus_profiles:
        return False
    if any(_on_cooldown(consensus_profile) for consensus_profile in consensus_profiles):
        return False
    min_score = min(trust_score(consensus_profile) for consensus_profile in consensus_profiles)
    probability = check_probability(min_score)
    return random.random() < probability  # noqa: S311 - not a cryptographic/security use


def record_check_result(profile: Profile, *, correct: bool) -> None:
    """Update ``profile``'s trust posterior after a trust-check round resolves.

    Race-safe: locks the profile's ``ConsensusProfile`` row for the duration
    of the update.
    """
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    with transaction.atomic():
        consensus_profile, _created = ConsensusProfile.objects.select_for_update().get_or_create(profile=profile)
        base_alpha = ConsensusProfile.DEFAULT_TRUST_ALPHA
        base_beta = ConsensusProfile.DEFAULT_TRUST_BETA
        consensus_profile.trust_alpha = TRUST_DECAY_GAMMA * (consensus_profile.trust_alpha - base_alpha) + base_alpha
        consensus_profile.trust_beta = TRUST_DECAY_GAMMA * (consensus_profile.trust_beta - base_beta) + base_beta
        if correct:
            consensus_profile.trust_alpha += 1.0
        else:
            consensus_profile.trust_beta += 1.0
        consensus_profile.checks_seen += 1
        consensus_profile.last_check_at = timezone.now()
        consensus_profile.save(update_fields=["trust_alpha", "trust_beta", "checks_seen", "last_check_at", "updated"])

    logger.info("Consensus trust-check for profile %s: %s (new trust_score=%.3f)", profile.pk, "passed" if correct else "failed", trust_score(consensus_profile))
