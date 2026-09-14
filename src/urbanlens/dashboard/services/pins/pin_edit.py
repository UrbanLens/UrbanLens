"""Shared pin field-edit/move/reparent/delete logic."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.icons import clean_icon
from urbanlens.dashboard.services.undo.handlers.pin import MODEL_LABEL as PIN_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from urbanlens.dashboard.models.labels.model import Label

logger = logging.getLogger(__name__)

#: Free-text fields normalized to ``None`` when submitted blank, so "cleared in the UI" and "explicit
#: JSON null" land on the same stored value.
#: Without this a pin edited on the website would hold ``""`` where the same edit from the mobile app
#: holds ``NULL``, and every ``field__isnull`` filter would disagree about which pins have one.
_TEXT_EDIT_FIELDS: frozenset[str] = frozenset({"name", "icon", "description", "color"})

#: Fields written through exactly as handed over; the caller's parser is
#: responsible for range/enum validation, which it can do far better (a form
#: silently keeps the old value, an API answers 400 naming the field).
_PASSTHROUGH_EDIT_FIELDS: frozenset[str] = frozenset(
    {
        "pin_type",
        "priority",
        "vulnerability",
        "danger",
        "last_visited",
        "date_built",
        "date_abandoned",
        "date_last_active",
    }
)

#: The 8 security indicators, taken from the model mixin rather than retyped so
#: a ninth added there is editable here without a second edit.
SECURITY_EDIT_FIELDS: frozenset[str] = frozenset(name for name, _label in SECURITY_FIELDS)

#: Every ``Pin`` field :func:`apply_pin_edits` will write.
#: Anything outside this set is refused rather than silently dropped - a caller naming a field this
#: module doesn't handle has a bug, and answering 200 to it is the exact silent-success failure this
#: module exists to prevent.
EDITABLE_PIN_FIELDS: frozenset[str] = _TEXT_EDIT_FIELDS | _PASSTHROUGH_EDIT_FIELDS | SECURITY_EDIT_FIELDS

#: Label kinds a pin-edit payload owns.
#: Person (``user``) and media labels are attached by entirely different surfaces (photo tagging,
#: media galleries), so a full label replacement submitted from a pin editor must leave them alone
#: rather than stripping labels its UI never showed the user.
ORGANIZE_LABEL_KINDS: tuple[str, ...] = (KIND_TAG, KIND_CATEGORY, KIND_STATUS)


class PinEditError(ValueError):
    """A submitted pin edit is self-contradictory or names a field we don't write."""


class UnknownPinFieldsError(PinEditError):
    """One or more submitted field names aren't in :data:`EDITABLE_PIN_FIELDS`."""


class ConflictingVisitedFieldsError(PinEditError):
    """``visited`` and an explicit ``last_visited`` were submitted together.
    The two make contradictory claims about the same fact, so neither may be allowed to silently win."""


class PinReparentError(ValueError):
    """The requested parent change is invalid."""


class ReparentLocationConflictError(PinReparentError):
    """Detaching would leave two top-level pins sharing one Location.
    Raised only when detaching (``new_parent=None``): the pin's own Location already has another top-level pin for this profile, and two root pins may never share one Location per profile."""


class CircularParentChainError(PinReparentError):
    """The requested parent is *pin* itself or one of its own descendants."""


class PinMoveError(ValueError):
    """The requested move can't be applied."""


class PinHasChildrenError(ValueError):
    """A delete was requested without saying what to do with the pin's children.

    Attributes:
        descendant_count: Size of the pin's subtree below it."""

    def __init__(self, pin: Pin, descendant_count: int, children_mode: str) -> None:
        self.descendant_count = descendant_count
        super().__init__(f"Pin {pin.pk} delete refused: {descendant_count} descendant(s) exist and children_mode={children_mode!r} is neither 'delete' nor 'keep'.")


def _normalize_text(value: Any) -> str | None:
    """Collapse a submitted free-text value to a stored value or ``None``.

    Args:
        value: The submitted value - a string, or ``None`` for an explicit clear.

    Returns:
        The trimmed string, or ``None`` when it was empty/whitespace-only.
    """
    return (str(value) if value is not None else "").strip() or None


def _replace_pin_labels(pin: Pin, labels: Sequence[Label]) -> None:
    """Make *labels* the pin's complete set of organize labels.

    Args:
        pin: The pin whose labels are being replaced.
        labels: The complete set the pin should end up with."""
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval

    keep_ids = {label.pk for label in labels}
    current = list(pin.labels.filter(kind__in=ORGANIZE_LABEL_KINDS))
    current_ids = {label.pk for label in current}

    removed = [label for label in current if label.pk not in keep_ids]
    for label in removed:
        PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LABEL, value=str(label.pk))
    if removed:
        pin.labels.remove(*removed)

    added = [label for label in labels if label.pk not in current_ids]
    if added:
        pin.labels.add(*added)


def apply_pin_edits(
    pin: Pin,
    fields: Mapping[str, Any],
    *,
    labels: Sequence[Label] | None = None,
    visited: bool | None = None,
) -> list[str]:
    """Apply a partial edit to *pin*, in one transaction, with its side effects.
    Absent means untouched: only the keys actually present in *fields* are written, so a single-control quick edit (a star click) cannot clobber a field the user never saw.

    Args:
        pin: The pin to edit, already known to belong to the caller.
        fields: ``Pin`` field name -> already-parsed value, containing only the fields this request actually submitted.
        labels: The pin's complete new organize-label set, or ``None`` to leave labels untouched.
        visited: ``True`` to mark the pin visited, ``False`` to un-mark it (which also clears ``last_visited``), or ``None`` to leave the marking alone.

    Returns:
        The ``Pin`` column names actually written, in submission order (the implicit companion flags included).

    Raises:
        UnknownPinFieldsError: *fields* names something outside :data:`EDITABLE_PIN_FIELDS`.
        ConflictingVisitedFieldsError: *visited* was combined with an explicit ``last_visited`` in the same call - silently letting one win is how a client ends up showing a visit date the server does not have."""
    unknown = sorted(set(fields) - EDITABLE_PIN_FIELDS)
    if unknown:
        raise UnknownPinFieldsError(f"apply_pin_edits received non-editable field(s): {', '.join(unknown)}.")
    if visited is not None and "last_visited" in fields:
        raise ConflictingVisitedFieldsError("apply_pin_edits received both `visited` and `last_visited` in one call.")

    update_fields: list[str] = []
    with transaction.atomic():
        for field, submitted in fields.items():
            value = _normalize_text(submitted) if field in _TEXT_EDIT_FIELDS else submitted
            if field == "icon":
                # Validated here rather than per-caller, since this is the one function behind both
                # the website's edit dialog and the API's PATCH.
                # `color` is cleaned by its callers instead because they each have their own default
                # to fall back to.
                value = clean_icon(value)
            setattr(pin, field, value)
            update_fields.append(field)
            if field == "name":
                pin.name_is_user_provided = bool(value)
                update_fields.append("name_is_user_provided")
            elif field == "pin_type":
                pin.pin_type_is_user_provided = True
                update_fields.append("pin_type_is_user_provided")

        if update_fields:
            pin.save(update_fields=[*update_fields, "updated"])

        if labels is not None:
            _replace_pin_labels(pin, labels)

        if visited is not None:
            # Imported here rather than at module scope: services.visits.visits imports
            # a good deal of the pin/label/memories graph, and this module is
            # imported by controllers that only ever move or delete a pin.
            from urbanlens.dashboard.services.visits.visits import add_visited_status, remove_visited_status

            if visited:
                add_visited_status(pin)
            else:
                remove_visited_status(pin)

    return update_fields


def move_pin_to_coordinates(pin: Pin, latitude: float, longitude: float) -> None:
    """Repoint *pin* to a new/existing Location at the given coordinates.
    Coordinates live on ``Location`` (not ``Pin``), so this repoints ``pin.location`` rather than writing through a serializer.

    Args:
        pin: The pin being moved.
        latitude: New latitude, already validated to be in range.
        longitude: New longitude, already validated to be in range.

    Raises:
        PinMoveError: The owner already has a *top-level* pin at that exact point."""
    from urbanlens.dashboard.models.location.model import Location

    location, _created = Location.objects.get_exact_or_create(latitude, longitude)

    # Only root pins are constrained - child pins are free to share a Location
    # with their parent and siblings, which is the whole point of detail pins.
    if pin.parent_pin_id is None and Pin.objects.filter(profile_id=pin.profile_id, location=location, parent_pin__isnull=True).exclude(pk=pin.pk).exists():
        raise PinMoveError(f"Pin {pin.pk} (profile {pin.profile_id}) already has a top-level pin at location {location.pk} ({latitude}, {longitude}).")

    before_lat, before_lng = float(pin.effective_latitude), float(pin.effective_longitude)
    pin.location = location
    pin.save(update_fields=["location", "updated"])
    from urbanlens.dashboard.services.undo.mutations import stash_pin_move

    stash_pin_move(pin, before_lat=before_lat, before_lng=before_lng, after_lat=latitude, after_lng=longitude)


def reparent_pin(pin: Pin, new_parent: Pin | None) -> None:
    """Change *pin*'s parent, enforcing the same invariants as the map UI.

    Args:
        pin: The pin to reparent.
        new_parent: The pin to become its new parent, or None to detach it to a top-level pin of its own.

    Raises:
        ReparentLocationConflictError: Detaching *pin* would leave it sharing its Location with another of this profile's top-level pins.
        CircularParentChainError: *new_parent* is *pin* itself or one of its own descendants."""
    if new_parent is None:
        if pin.parent_pin_id is None:
            return
        conflict = Pin.objects.filter(profile=pin.profile, location_id=pin.location_id, parent_pin__isnull=True).exclude(pk=pin.pk).exists()
        if conflict:
            raise ReparentLocationConflictError(f"Pin {pin.pk} (location {pin.location_id}) already shares that location with another top-level pin of profile {pin.profile_id}; refusing detach.")
        pin.parent_pin = None
    else:
        if pin.would_create_cycle(new_parent):
            raise CircularParentChainError(f"Reparenting pin {pin.pk} under pin {new_parent.pk} would create a cycle.")
        pin.parent_pin = new_parent
    pin.save(update_fields=["parent_pin", "updated"])


def _promote_children(instance: Pin) -> list[int]:
    """Re-parent *instance*'s direct children ahead of its deletion.
    A child whose Location already has another top-level pin nests under that pin instead (top-level pins are unique per Location+profile).

    Args:
        instance: The pin about to be deleted.

    Returns:
        Primary keys of children whose promotion must finish post-delete."""
    new_parent_id = instance.parent_pin_id
    deferred_ids: list[int] = []
    for child in Pin.objects.filter(parent_pin=instance):
        if new_parent_id is not None:
            child.parent_pin_id = new_parent_id
            child.save(update_fields=["parent_pin", "updated"])
            continue
        other_root = Pin.objects.filter(profile_id=instance.profile_id, location_id=child.location_id, parent_pin__isnull=True).exclude(pk=instance.pk).first()
        if other_root is not None:
            child.parent_pin_id = other_root.pk
            child.save(update_fields=["parent_pin", "updated"])
        elif child.location_id == instance.location_id:
            # Bypass save() so no side effects run for this transient state.
            Pin.objects.filter(pk=child.pk).update(parent_pin_id=child.pk)
            deferred_ids.append(child.pk)
        else:
            child.parent_pin = None
            child.save(update_fields=["parent_pin", "updated"])
    return deferred_ids


def _finish_deferred_promotions(profile_id: int, deferred_ids: list[int]) -> None:
    """Finish promoting the children held back by :func:`_promote_children`.
    Runs after the parent pin's row is gone, so its root slot is free.

    Args:
        profile_id: Owner of the pins (root uniqueness is per profile).
        deferred_ids: Primary keys of the temporarily self-parented children."""
    for child in Pin.objects.filter(pk__in=deferred_ids):
        existing_root = Pin.objects.filter(profile_id=profile_id, location_id=child.location_id, parent_pin__isnull=True).exclude(pk=child.pk).first()
        child.parent_pin_id = existing_root.pk if existing_root is not None else None
        child.save(update_fields=["parent_pin", "updated"])


@dataclass(frozen=True, slots=True)
class PinDeletion:
    """The pins actually removed by :func:`delete_pin`."""

    deleted: list[Pin]


def delete_pin(pin: Pin, *, children_mode: str = "") -> PinDeletion:
    """Delete *pin*, asking the caller what to do with its child pins first.

    Args:
        pin: The pin to delete.
        children_mode: ``"delete"``, ``"keep"``, or ``""`` when the pin is known to have no descendants.

    Returns:
        The pins actually deleted (just ``pin`` in "keep" mode, the whole subtree otherwise).

    Raises:
        PinHasChildrenError: *pin* has descendants and ``children_mode`` is neither ``"delete"`` nor ``"keep"``."""
    subtree = list(Pin.objects.filter(pk=pin.pk).with_descendants())
    descendant_count = len(subtree) - 1

    if descendant_count and children_mode not in {"delete", "keep"}:
        raise PinHasChildrenError(pin, descendant_count, children_mode)

    with transaction.atomic():
        if descendant_count and children_mode == "keep":
            deferred_ids = _promote_children(pin)
            deleted = [pin]
            stash_for_undo(PIN_MODEL_LABEL, deleted, pin.profile)
            pin.delete()
            _finish_deferred_promotions(pin.profile_id, deferred_ids)
        else:
            deleted = subtree
            stash_for_undo(PIN_MODEL_LABEL, deleted, pin.profile)
            for descendant in subtree:
                descendant.delete()
    return PinDeletion(deleted=deleted)
