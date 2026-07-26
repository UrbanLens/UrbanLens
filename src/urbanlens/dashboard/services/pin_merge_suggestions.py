"""Accept/reject lifecycle for PinMergeSuggestion.

Thin wrapper around ``services.pin_merge.merge_pins`` - this module only
resolves "which pin is the survivor" and flips the suggestion's status;
``pin_merge`` owns the actual data-consolidating merge mechanics so any future
"merge these two pins" affordance can call it directly without going through a
suggestion at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionStatus
from urbanlens.dashboard.services.pin_merge import merge_pins

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

#: No merge-specific undo: reversing a merge would mean un-repointing every
#: relation merge_pins just moved, which the shared undo-stash framework has
#: no mechanism for (it only restores a deleted instance's own fields - see
#: services.undo.handlers.pin.PinUndoHandler's docstring). Accepting a merge
#: suggestion is final; the UI must say so plainly before the user confirms.


def _resolve_survivor_loser(suggestion: PinMergeSuggestion, survivor_pk: int | None) -> tuple[Pin, Pin]:
    """Resolve which of a suggestion's two pins is the survivor vs. the loser.

    Args:
        suggestion: The suggestion being accepted.
        survivor_pk: Explicit choice of pin_a_id/pin_b_id, or None to use
            ``suggestion.suggested_survivor``.

    Returns:
        (survivor, loser) pins.

    Raises:
        ValueError: ``survivor_pk`` (or ``suggested_survivor``, if that's what
            was used) is neither of this suggestion's two pins, or either pin
            is already gone (only possible for a non-pending suggestion, which
            the caller should have already filtered out via ``is_actionable``).
    """
    chosen_pk = survivor_pk if survivor_pk is not None else suggestion.suggested_survivor_id
    if chosen_pk not in (suggestion.pin_a_id, suggestion.pin_b_id):
        raise ValueError(f"{chosen_pk} is not one of this suggestion's two pins")
    survivor = suggestion.pin_a if chosen_pk == suggestion.pin_a_id else suggestion.pin_b
    loser = suggestion.pin_b if chosen_pk == suggestion.pin_a_id else suggestion.pin_a
    if survivor is None or loser is None:
        raise ValueError(f"PinMergeSuggestion {suggestion.pk} is missing one of its pins")
    return survivor, loser


def accept_pin_merge_suggestion(
    suggestion: PinMergeSuggestion,
    profile: Profile,
    *,
    survivor_pk: int | None = None,
    resolutions: dict[str, int] | None = None,
) -> Pin:
    """Accept a pending merge suggestion, merging one pin into the other.

    Args:
        suggestion: The pending suggestion being accepted.
        profile: The accepting profile (must own both pins).
        survivor_pk: User's explicit choice of which pin survives - must be
            ``suggestion.pin_a_id`` or ``suggestion.pin_b_id``. Defaults to
            ``suggestion.suggested_survivor`` when omitted.
        resolutions: User's choices for any real field conflicts between the
            two pins - see ``services.pin_merge.plan_merge_conflicts`` and
            ``merge_pins``.

    Returns:
        The surviving, now-merged pin.

    Raises:
        ValueError: ``survivor_pk`` isn't one of the suggestion's two pins.
        services.pin_merge.UnresolvedMergeConflictError: a real conflict
            between the two pins has no resolution supplied.
    """
    survivor, loser = _resolve_survivor_loser(suggestion, survivor_pk)
    merged = merge_pins(survivor, loser, profile, resolutions)
    # A plain queryset .update() rather than suggestion.save(): survivor/loser
    # are suggestion.pin_a/pin_b themselves, and merge_pins just deleted loser -
    # save()'s related-field check would refuse to persist a suggestion still
    # holding a cached (now-unsaved) reference to the pin it just deleted.
    now = timezone.now()
    PinMergeSuggestion.objects.filter(pk=suggestion.pk).update(status=PinMergeSuggestionStatus.ACCEPTED, updated=now)
    suggestion.status = PinMergeSuggestionStatus.ACCEPTED
    suggestion.updated = now
    return merged


def reject_pin_merge_suggestion(suggestion: PinMergeSuggestion) -> None:
    """Reject a pending PinMergeSuggestion - neither pin is touched.

    Args:
        suggestion: The pending suggestion being rejected.
    """
    suggestion.status = PinMergeSuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated"])
