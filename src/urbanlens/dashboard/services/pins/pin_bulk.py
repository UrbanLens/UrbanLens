"""Multi-select bulk actions on pins: merge under a target, delete with undo, and a shared edit.

The map's select tool and the external API both call these, so the two cannot drift. Each action is one
transaction, and hierarchy changes refit each affected parent's child-fitted boundary once at the end rather than
once per moved pin (``deferring_child_boundary_refits``).

Callers resolve ownership and parse their own request shapes; everything handed in here is already the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import TYPE_CHECKING, Final, Literal

from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.geo.child_pin_boundaries import deferring_child_boundary_refits
from urbanlens.dashboard.services.pins.pin_edit import PinReparentError, reparent_pin

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.undo import UndoAction

logger = logging.getLogger(__name__)

#: Most pins one bulk request may name; these edits are per-row saves, not one ``UPDATE``.
MAX_BULK_PINS = 500


class Unset(Enum):
    """Sentinel type for :data:`UNSET`."""

    UNSET = "unset"


#: "Leave this alone", distinct from ``None`` ("clear it").
UNSET: Final = Unset.UNSET


class BulkPinError(ValueError):
    """A bulk action was refused as a whole; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MergeTargetConflictError(BulkPinError):
    """Promoting a child-pin target would put two top-level pins on one Location."""

    def __init__(self) -> None:
        super().__init__("You already have a top-level pin at this exact location. Choose a different pin as the merge target.")


class NoValidSourcesError(BulkPinError):
    """Nothing left to merge once the target is excluded."""

    def __init__(self) -> None:
        super().__init__("No valid source pins.")


class MergeCycleError(BulkPinError):
    """Every source would have become its own ancestor."""

    def __init__(self) -> None:
        super().__init__("Merge would create a cycle.")


@dataclass(frozen=True, slots=True)
class BulkMergeResult:
    """What :func:`bulk_merge_under` did."""

    target: Pin
    merged: list[Pin]
    skipped: list[Pin]


@dataclass(frozen=True, slots=True)
class BulkDeleteResult:
    """What :func:`bulk_delete_pins` removed, and the undo entry that restores it."""

    deleted: list[Pin]
    subtree: list[Pin]
    undo_action: UndoAction

    @property
    def descendant_count(self) -> int:
        """Pins removed only because an ancestor was selected."""
        return len(self.subtree) - len(self.deleted)


@dataclass(frozen=True, slots=True)
class BulkPinEdit:
    """One edit applied to every selected pin; :data:`UNSET` leaves a part alone.

    Attributes:
        description: New description; ``None`` clears it.
        style: Already-validated marker style columns to write (``icon``, ``color``, ``detail_*``).
        rating: 1-5 sets the caller's review rating; ``None`` deletes their review.
        add_labels: Organize labels to attach, already scoped to what the caller may use.
        remove_labels: Organize labels to detach (tombstoned so auto-tagging does not re-add them).
        parent: New parent pin; ``None`` detaches to top level.
    """

    description: str | None | Literal[Unset.UNSET] = UNSET
    style: Mapping[str, str | int | None] = field(default_factory=dict)
    rating: int | None | Literal[Unset.UNSET] = UNSET
    add_labels: Sequence[Label] = ()
    remove_labels: Sequence[Label] = ()
    parent: Pin | None | Literal[Unset.UNSET] = UNSET


@dataclass(frozen=True, slots=True)
class BulkEditResult:
    """What :func:`bulk_edit_pins` changed."""

    count: int
    reparented: int


def bulk_merge_under(target: Pin, sources: Sequence[Pin]) -> BulkMergeResult:
    """Make every source a detail pin of *target*, promoting *target* to top level first if it is a child.

    Args:
        target: The pin that ends up on top.
        sources: The pins to fold under it (the target itself is ignored if included).

    Returns:
        The merged and skipped (would-be-cycle) sources.

    Raises:
        NoValidSourcesError: No source other than the target.
        MergeTargetConflictError: The target is a child pin and its Location already has another top-level pin.
        MergeCycleError: Every source was skipped; nothing, including the promotion, is kept.
    """
    candidates = [source for source in sources if source.pk != target.pk]
    if not candidates:
        raise NoValidSourcesError
    promote = target.parent_pin_id is not None
    if promote and Pin.objects.filter(profile_id=target.profile_id, location_id=target.location_id, parent_pin__isnull=True).exclude(pk=target.pk).exists():
        raise MergeTargetConflictError

    merged: list[Pin] = []
    skipped: list[Pin] = []
    with transaction.atomic(), deferring_child_boundary_refits():
        if promote:
            target.parent_pin = None
            target.save(update_fields=["parent_pin", "updated"])
        for source in candidates:
            try:
                reparent_pin(source, target)
            except PinReparentError:
                skipped.append(source)
                continue
            merged.append(source)
        if not merged:
            raise MergeCycleError
    return BulkMergeResult(target=target, merged=merged, skipped=skipped)


def bulk_delete_pins(profile: Profile, pins: Sequence[Pin]) -> BulkDeleteResult:
    """Delete *pins* and their whole detail-pin subtrees, staging one undo entry for all of it.

    Args:
        profile: The owner, who may restore the deletion.
        pins: The selected pins.

    Returns:
        The selected pins, the full subtree removed, and the undo entry.
    """
    from urbanlens.dashboard.services.undo.handlers.pin import MODEL_LABEL as PIN_MODEL_LABEL
    from urbanlens.dashboard.services.undo.service import stash_for_undo

    pks = [pin.pk for pin in pins]
    with transaction.atomic(), deferring_child_boundary_refits():
        subtree = list(Pin.objects.filter(pk__in=pks).with_descendants())
        # Stashed in the same transaction as the delete, so a failed delete cannot leave an undo for nothing.
        undo_action = stash_for_undo(PIN_MODEL_LABEL, subtree, profile)
        if undo_action is None:
            raise RuntimeError("stash_for_undo returned None outside an apply")
        Pin.objects.filter(pk__in=pks).delete()
    return BulkDeleteResult(deleted=list(pins), subtree=subtree, undo_action=undo_action)


def bulk_edit_pins(profile: Profile, pins: Sequence[Pin], edit: BulkPinEdit) -> BulkEditResult:
    """Apply *edit* to every pin, all or nothing.

    Args:
        profile: The owner, whose review a rating writes.
        pins: The selected pins.
        edit: What to change.

    Returns:
        How many pins were selected and how many actually changed parent.
    """
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
    from urbanlens.dashboard.models.reviews.model import Review

    reparented = 0
    with transaction.atomic(), deferring_child_boundary_refits():
        columns: dict[str, str | int | None] = dict(edit.style)
        if edit.description is not UNSET:
            columns["description"] = (edit.description or "").strip() or None
        if columns:
            update_fields = [*columns, "updated"]
            for pin in pins:
                for name, value in columns.items():
                    setattr(pin, name, value)
                pin.save(update_fields=update_fields)

        if edit.rating is None:
            Review.objects.filter(profile=profile, pin__in=pins).delete()
        elif edit.rating is not UNSET:
            for pin in pins:
                Review.objects.update_or_create(profile=profile, pin=pin, defaults={"rating": edit.rating})

        if edit.add_labels:
            for pin in pins:
                pin.labels.add(*edit.add_labels)

        if edit.remove_labels:
            remove_ids = {label.pk for label in edit.remove_labels}
            attached = set(Pin.labels.through.objects.filter(pin__in=pins, label_id__in=remove_ids).values_list("pin_id", "label_id"))
            for pin in pins:
                present = [label for label in edit.remove_labels if (pin.pk, label.pk) in attached]
                if not present:
                    continue
                for label in present:
                    PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LABEL, value=str(label.pk))
                pin.labels.remove(*present)

        if edit.parent is not UNSET:
            for pin in pins:
                if edit.parent is None and pin.parent_pin_id is None:
                    continue
                if edit.parent is not None and pin.pk == edit.parent.pk:
                    continue
                try:
                    reparent_pin(pin, edit.parent)
                except PinReparentError:
                    continue
                reparented += 1
    return BulkEditResult(count=len(pins), reparented=reparented)
