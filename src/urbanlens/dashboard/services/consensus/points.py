"""Points, leveling, and point-award constants for Consensus.

Consensus-only - not shared with SpotGuessr/Trivia's Glicko-2 ratings (see
``models.consensus.model.ConsensusProfile``). Leveling uses a logarithmic
cost-density curve (see ``points_required_for_level``) so leveling up never
becomes free, but stays achievable even at high levels.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from collections.abc import Mapping

    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

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

#: What a change to something other than a substantive wiki field is worth -
#: an alias, a link, a markup map, an imported child wiki. These are real
#: contributions but far cheaper to make than writing a description, and they
#: used to earn the same 3 as one.
MANUAL_EDIT_EXTRA_POINTS = 1

#: Ceiling on a single edit's award, however many fields it touched. One dialog
#: submit can change every editable field at once, and without a cap that would
#: out-earn a whole Consensus round. Held below ``SOLO_ANSWER_POINTS`` by
#: ``test_consensus_points``, so a later retune cannot quietly make editing the
#: best points-per-effort path in the game - which is the opposite of what the
#: award comment above says this is for.
MANUAL_EDIT_POINTS_CAP = 6


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


def points_for_changes(changes: Mapping[str, object] | None) -> int:
    """Value one wiki edit's diff, by what it actually changed.

    A first cut, and deliberately a coarse one: substantive wiki fields are
    worth :data:`MANUAL_EDIT_POINTS` each, everything else
    :data:`MANUAL_EDIT_EXTRA_POINTS`, and the total is capped. The point is not
    to price contributions accurately - it is that an alias and a rewritten
    description used to be worth the same, and that a single dialog submit
    touching every field used to pay per field with no ceiling.

    Unrecognised keys fall to the cheaper tier rather than the dearer one, so
    a new edit kind cannot become the best rate in the game by being added.

    Args:
        changes: A ``WikiEdit.changes`` diff, or None.

    Returns:
        Points in ``[0, MANUAL_EDIT_POINTS_CAP]``.
    """
    from urbanlens.dashboard.services.wiki.wiki_edits import WIKI_EDITABLE_FIELDS

    if not changes:
        return 0
    substantive = set(WIKI_EDITABLE_FIELDS)
    total = 0
    for key in changes:
        is_substantive = key in substantive or key == "bounding_box" or key.startswith("boundary_")
        total += MANUAL_EDIT_POINTS if is_substantive else MANUAL_EDIT_EXTRA_POINTS
    return min(total, MANUAL_EDIT_POINTS_CAP)


def points_for_wiki_edit(edit: WikiEdit) -> int:
    """What ``edit`` should pay its editor.

    Zero for the four cases that must never earn: a revert (undoing somebody
    else's work is not a contribution, and paying for it means an edit war pays
    both sides on every pass), a Consensus-sourced edit (already paid, more, at
    round resolution), an edit whose editor row is gone, and an empty diff.

    Args:
        edit: The edit to value.

    Returns:
        Points to award, possibly 0.
    """
    if edit.is_revert or edit.consensus_round_id is not None or edit.editor_id is None:
        return 0
    return points_for_changes(edit.changes)


def _adjust_points(profile_id: int, delta: int, *, reason: str) -> None:
    """Move a profile's lifetime total by ``delta`` and recompute its level.

    Unlike :func:`award_points` this never creates a ``ConsensusProfile``: a
    retraction for a profile that has no row is a no-op, not a reason to
    materialise one at zero.

    Args:
        profile_id: The profile whose total moves.
        delta: Signed points. Negative deltas clamp at zero - ``total_points``
            is a ``PositiveIntegerField``, so a legacy row awarded under
            different weights must not be able to drive it negative.
        reason: Short machine-readable label, for the log line.
    """
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    with transaction.atomic():
        consensus_profile = ConsensusProfile.objects.select_for_update().filter(profile_id=profile_id).first()
        if consensus_profile is None:
            return
        consensus_profile.total_points = max(0, consensus_profile.total_points + delta)
        consensus_profile.level = level_for_points(consensus_profile.total_points)
        consensus_profile.save(update_fields=["total_points", "level", "updated"])
    logger.info("Adjusted Consensus points for profile %s by %s (%s)", profile_id, delta, reason)


def record_wiki_edit_award(edit: WikiEdit) -> None:
    """Pay ``edit``'s editor and record on the row what was paid.

    The amount is stored rather than left to be recomputed later because the
    weights above are expected to be retuned, and
    :func:`retract_wiki_edit_award` has to return exactly what this paid - not
    what the same diff would earn under whatever weights are current then.

    Args:
        edit: The freshly created edit.
    """
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit as WikiEditModel

    amount = points_for_wiki_edit(edit)
    if not amount or edit.editor_id is None:
        return
    award_points(edit.editor_id, amount, reason="manual_wiki_edit")
    # queryset update, never save(): this runs inside post_save.
    WikiEditModel.objects.filter(pk=edit.pk).update(consensus_points=amount)
    edit.consensus_points = amount


def retract_wiki_edit_award(edit: WikiEdit) -> bool:
    """Take back what ``edit`` paid, once.

    Compare-and-swap on ``consensus_points_retracted``, the same shape
    ``services.reputation.scoring.retract_event`` uses and for the same reason:
    several paths can reach this for one row (the revert itself, an admin
    toggling the flag, deleting an already-reverted edit), and only the first
    may move the total.

    Args:
        edit: The edit whose award is being withdrawn.

    Returns:
        Whether this call changed anything.
    """
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit as WikiEditModel

    if edit.editor_id is None or not edit.consensus_points:
        return False
    updated = WikiEditModel.objects.filter(pk=edit.pk, consensus_points_retracted=False).update(consensus_points_retracted=True)
    if not updated:
        return False
    edit.consensus_points_retracted = True
    _adjust_points(edit.editor_id, -edit.consensus_points, reason="wiki_edit_reverted")
    return True


def restore_wiki_edit_award(edit: WikiEdit) -> bool:
    """Put back an award a later revert took away - the revert-of-a-revert case.

    Args:
        edit: The edit whose award is being reinstated.

    Returns:
        Whether this call changed anything.
    """
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit as WikiEditModel

    if edit.editor_id is None or not edit.consensus_points:
        return False
    updated = WikiEditModel.objects.filter(pk=edit.pk, consensus_points_retracted=True).update(consensus_points_retracted=False)
    if not updated:
        return False
    edit.consensus_points_retracted = False
    _adjust_points(edit.editor_id, edit.consensus_points, reason="wiki_edit_revert_undone")
    return True


def restore_consensus_points_for(edit_ids: list[int]) -> None:
    """Reinstate awards for edits whose revert was itself reverted.

    The batch form, called directly rather than left to the signal for the same
    reason ``services.wiki.wiki_edits._restore_reputation_for`` is: the caller
    clears the flag with a queryset ``update()``, which emits no ``post_save``.

    Args:
        edit_ids: WikiEdit pks whose reverts have just been undone.
    """
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit as WikiEditModel

    for edit in WikiEditModel.objects.filter(pk__in=edit_ids, consensus_points_retracted=True):
        restore_wiki_edit_award(edit)


def backfill_wiki_edit_points(wiki_edit_model: type[WikiEdit]) -> None:
    """Record on every existing row what it was actually paid.

    Extracted from the migration that calls it so it can be exercised by a test
    - this repo has no migration-test harness, so logic left inline in a
    ``RunPython`` is logic nothing runs until deploy.

    Every legacy row was paid a flat :data:`MANUAL_EDIT_POINTS`, whatever it
    changed, so that is what is recorded - not what the new weights would give
    it. Rows that are some other row's ``reverted_by`` are marked
    ``is_revert``, but their award is left in place: draining points people
    have already been shown is a bigger change than declining to pay new ones,
    and is not what this is for.

    Args:
        wiki_edit_model: The historical ``WikiEdit`` model from the migration.
    """
    # A revert row is one some other row names as its `reverted_by` - i.e. one
    # whose reverse `reverts` set is non-empty. Collected as ids first because
    # update() cannot follow a reverse relation.
    reverting_ids = list(wiki_edit_model.objects.filter(reverts__isnull=False).values_list("pk", flat=True).distinct())
    if reverting_ids:
        wiki_edit_model.objects.filter(pk__in=reverting_ids).update(is_revert=True)
    wiki_edit_model.objects.filter(editor__isnull=False, consensus_round__isnull=True, is_revert=False).update(consensus_points=MANUAL_EDIT_POINTS)
