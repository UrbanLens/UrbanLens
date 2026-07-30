"""Points, leveling, and point-award constants for Consensus.

Consensus-only - not shared with SpotGuessr/Trivia's Glicko-2 ratings (see
``models.consensus.model.ConsensusProfile``). Leveling uses a logarithmic
cost-density curve (see ``points_required_for_level``) so leveling up never
becomes free, but stays achievable even at high levels.
"""

from __future__ import annotations

import logging
import math

from django.db import transaction

logger = logging.getLogger(__name__)

#: Scale constant for the leveling curve - see ``points_required_for_level``.
LEVEL_SCALE_K = 100.0

#: Sane upper bound on level, purely to bound level_for_points's loop -
#: no player will realistically reach it, but an unbounded while loop
#: shouldn't exist regardless.
MAX_LEVEL = 500

#: Point awards, by how the point was earned. Solo answers earn less than a
#: competitive vote win (a competitive win reflects the extra work of
#: convincing other players), but every path earns *something* so no
#: contribution feels wasted - including a plain out-of-game wiki edit,
#: which is deliberately worth less than any in-game path so playing the
#: game is still the primary way to rack up points.
SOLO_ANSWER_POINTS = 10
COMPETITIVE_AGREE_POINTS = 15
VOTE_WINNER_POINTS = 20
VOTE_PARTICIPANT_POINTS = 5
#: Fixed per the Consensus design spec - "just 1 point apiece" when a
#: competitive round's vote fails to reach consensus.
TENTATIVE_POINTS = 1
MANUAL_EDIT_POINTS = 3
PHOTO_UPLOAD_BONUS_POINTS = 5


def points_required_for_level(level: int) -> int:
    """Cumulative lifetime points required to advance from ``level`` to ``level + 1``.

    ``threshold(n) = round(K * n * ln(n + 1))`` - absolute per-level cost
    keeps rising (leveling up never becomes free), but cost-*density*
    ``threshold(n) / n = K * ln(n + 1)`` grows only logarithmically, so
    going from level 10 to level 100 costs roughly 19x, not the ~100x a
    quadratic curve (or the unreachable multiple an exponential curve) would
    demand - "harder every level, but achievable even at high levels," per
    the design spec.

    Args:
        level: The level being advanced *from* (1-indexed). Levels below 1
            require no points - every profile starts at level 1 for free.

    Returns:
        Points required, or 0 for ``level < 1``.
    """
    if level < 1:
        return 0
    return round(LEVEL_SCALE_K * level * math.log(level + 1))


def level_for_points(points: int) -> int:
    """The level ``points`` lifetime Consensus points corresponds to.

    Every profile starts at level 1 (free); each subsequent level costs
    ``points_required_for_level(current_level)`` more, per the curve above.
    """
    level = 1
    while level < MAX_LEVEL and points >= points_required_for_level(level):
        level += 1
    return level


def award_points(profile_id: int, amount: int, *, reason: str) -> bool:
    """Award ``amount`` lifetime Consensus points to ``profile_id``, recomputing their level.

    Race-safe: locks the profile's ``ConsensusProfile`` row for the duration
    of the update, so two concurrent awards (e.g. two rounds resolving at
    once) can't read-modify-write past each other.

    Args:
        profile_id: The profile earning points.
        amount: Points to add (may be 0, though callers shouldn't bother).
        reason: A short machine-readable label for logging (e.g.
            ``"solo_answer"``, ``"manual_wiki_edit"``) - not persisted
            anywhere yet, just surfaced in the log line below.

    Returns:
        True if this award pushed the profile to a new level.
    """
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    with transaction.atomic():
        consensus_profile, _ = ConsensusProfile.objects.select_for_update().get_or_create(profile_id=profile_id)
        previous_level = consensus_profile.level
        consensus_profile.total_points += amount
        consensus_profile.level = level_for_points(consensus_profile.total_points)
        consensus_profile.save(update_fields=["total_points", "level", "updated"])

    leveled_up = consensus_profile.level > previous_level
    logger.info("Awarded %s Consensus points to profile %s (%s)%s", amount, profile_id, reason, " - leveled up!" if leveled_up else "")
    return leveled_up


def award_points_for_manual_edit(editor_id: int) -> None:
    """Award the baseline out-of-game point value for a plain (non-Consensus) wiki edit.

    Called by ``models.wiki_edit.signals`` for every ``WikiEdit`` not
    produced by Consensus itself - see that signal's docstring for the
    double-award guard.
    """
    award_points(editor_id, MANUAL_EDIT_POINTS, reason="manual_wiki_edit")
