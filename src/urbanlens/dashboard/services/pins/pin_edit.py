"""Shared pin field-edit/move/reparent/delete logic.

This is the single code path behind editing, moving, reparenting, and deleting
a Pin, shared by the website's HTMX edit dialog
(``controllers.pin_edit.PinEditView``), the internal DRF surface
(``models.pin.viewset.PinViewSet``) and the external API
(``external_api.views.PinDetailView``) - mirrors the same one-implementation
principle as ``services.pins.pin_creation`` for pin creation.

The split of responsibilities is deliberate. Each caller owns *parsing* its own
wire format - a browser form posts blank strings and comma-joined names, a JSON
API posts typed values validated by a DRF serializer - and hands this module a
mapping of already-coerced values keyed by ``Pin`` field name. This module owns
everything that happens *after* that: which companion flags a field write
implies, which ``update_fields`` the save must name (the wiki-stat signals key
off it), and the tombstoning that makes a label removal stick. Those are the
parts that silently drift when two surfaces each grow their own copy.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.handlers.pin import MODEL_LABEL as PIN_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from urbanlens.dashboard.models.labels.model import Label

logger = logging.getLogger(__name__)

#: Free-text fields normalized to ``None`` when submitted blank, so "cleared in
#: the UI" and "explicit JSON null" land on the same stored value. Without this
#: a pin edited on the website would hold ``""`` where the same edit from the
#: mobile app holds ``NULL``, and every ``field__isnull`` filter would disagree
#: about which pins have one.
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

#: Every ``Pin`` field :func:`apply_pin_edits` will write. Anything outside this
#: set is refused rather than silently dropped - a caller naming a field this
#: module doesn't handle has a bug, and answering 200 to it is the exact
#: silent-success failure this module exists to prevent.
EDITABLE_PIN_FIELDS: frozenset[str] = _TEXT_EDIT_FIELDS | _PASSTHROUGH_EDIT_FIELDS | SECURITY_EDIT_FIELDS

#: Label kinds a pin-edit payload owns. Person (``user``) and media labels are
#: attached by entirely different surfaces (photo tagging, media galleries), so
#: a full label replacement submitted from a pin editor must leave them alone
#: rather than stripping labels its UI never showed the user.
ORGANIZE_LABEL_KINDS: tuple[str, ...] = (KIND_TAG, KIND_CATEGORY, KIND_STATUS)


class PinEditError(ValueError):
    """A submitted pin edit is self-contradictory or names a field we don't write.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class PinReparentError(ValueError):
    """The requested parent change is invalid.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class PinMoveError(ValueError):
    """The requested move can't be applied.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class PinHasChildrenError(ValueError):
    """A delete was requested without saying what to do with the pin's children.

    Callers should ask the user, then retry with an explicit ``children_mode``.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, descendant_count: int) -> None:
        self.descendant_count = descendant_count
        self.safe_message = "This pin has child pins - specify children_mode='delete' or 'keep'."
        super().__init__(self.safe_message)


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

    Every label dropped by the replacement gets a ``PinAutoRemoval`` tombstone
    first. Without it the removal does not stick: keyword and AI auto-tagging
    both re-derive labels from the pin's own text, so the next time either runs
    over this pin it would reattach the exact label the user just took off, and
    the user would watch it come back on its own. This mirrors
    ``controllers.labels.LabelPinMembershipView``'s remove branch, which records
    the same ``(pin, LABEL, primary key)`` tuple for a single-label removal.

    Only :data:`ORGANIZE_LABEL_KINDS` participate - see its docstring.

    Args:
        pin: The pin whose labels are being replaced.
        labels: The complete set the pin should end up with. Callers are
            responsible for having resolved these to labels the owner may use.
    """
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

    Absent means untouched: only the keys actually present in *fields* are
    written, so a single-control quick edit (a star click) cannot clobber a
    field the user never saw. That also keeps ``update_fields`` narrow, which
    matters beyond query size - ``models.pin.signals.sync_pin_stats_to_wiki``
    reads it to decide which community ``WikiStatVote`` rows to publish, so a
    save that names every field would republish the owner's priority/danger/
    vulnerability votes on every unrelated edit.

    Two companion flags are written implicitly, because a write that forgets
    them is worse than no write at all:

    * ``name`` sets ``name_is_user_provided``, so a name the user typed is not
      later overwritten by an external-API name refresh.
    * ``pin_type`` sets ``pin_type_is_user_provided`` to True, so automatic
      building/parcel classification (``services.locations.site_scope``) stops
      overruling the type the user deliberately chose. Submitting the value it
      already had still counts: the user looked at the control and confirmed it.

    Args:
        pin: The pin to edit, already known to belong to the caller.
        fields: ``Pin`` field name -> already-parsed value, containing only the
            fields this request actually submitted. Must be a subset of
            :data:`EDITABLE_PIN_FIELDS`.
        labels: The pin's complete new organize-label set, or ``None`` to leave
            labels untouched. See :func:`_replace_pin_labels` for the tombstones
            a removal writes.
        visited: ``True`` to mark the pin visited, ``False`` to un-mark it (which
            also clears ``last_visited``), or ``None`` to leave the marking
            alone. Applied after *labels* so a replacement set that happens to
            omit the "Visited" status label doesn't undo an explicit
            ``visited: true`` in the same request.

    Returns:
        The ``Pin`` column names actually written, in submission order (the
        implicit companion flags included). Empty when *fields* was empty.

    Raises:
        PinEditError: *fields* names something outside
            :data:`EDITABLE_PIN_FIELDS`, or combines *visited* with an explicit
            ``last_visited`` - the two make contradictory claims about the same
            fact and silently letting one win is how a client ends up showing a
            visit date the server does not have.
    """
    unknown = sorted(set(fields) - EDITABLE_PIN_FIELDS)
    if unknown:
        raise PinEditError(f"These pin fields are not editable: {', '.join(unknown)}.")
    if visited is not None and "last_visited" in fields:
        raise PinEditError("Send either 'visited' or 'last_visited', not both - they disagree about the same fact.")

    update_fields: list[str] = []
    with transaction.atomic():
        for field, submitted in fields.items():
            value = _normalize_text(submitted) if field in _TEXT_EDIT_FIELDS else submitted
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

    Coordinates live on ``Location`` (not ``Pin``), so this repoints
    ``pin.location`` rather than writing through a serializer. Resolution is
    exact (``get_exact_or_create``): a manual move lands on the point the user
    actually submitted rather than snapping to whatever Location happens to sit
    within the default dedup radius.

    Args:
        pin: The pin being moved.
        latitude: New latitude, already validated to be in range.
        longitude: New longitude, already validated to be in range.

    Raises:
        PinMoveError: The owner already has a *top-level* pin at that exact
            point. One root pin per Location per profile is a database
            constraint, so without this check the move surfaces as an
            unhandled IntegrityError.
    """
    from urbanlens.dashboard.models.location.model import Location

    location, _created = Location.objects.get_exact_or_create(latitude, longitude)

    # Only root pins are constrained - child pins are free to share a Location
    # with their parent and siblings, which is the whole point of detail pins.
    if pin.parent_pin_id is None and Pin.objects.filter(profile_id=pin.profile_id, location=location, parent_pin__isnull=True).exclude(pk=pin.pk).exists():
        raise PinMoveError("You already have a pin at these exact coordinates.")

    pin.location = location
    pin.save(update_fields=["location", "updated"])


def reparent_pin(pin: Pin, new_parent: Pin | None) -> None:
    """Change *pin*'s parent, enforcing the same invariants as the map UI.

    Args:
        pin: The pin to reparent.
        new_parent: The pin to become its new parent, or None to detach it
            to a top-level pin of its own.

    Raises:
        PinReparentError: The change would create a cycle, or (when detaching)
            *pin*'s own Location already has another top-level pin for this
            profile - two root pins can't share one Location per profile.
    """
    if new_parent is None:
        if pin.parent_pin_id is None:
            return
        conflict = Pin.objects.filter(profile=pin.profile, location_id=pin.location_id, parent_pin__isnull=True).exclude(pk=pin.pk).exists()
        if conflict:
            raise PinReparentError("You already have a top-level pin at this exact location. Move this pin before detaching it.")
        pin.parent_pin = None
    else:
        if pin.would_create_cycle(new_parent):
            raise PinReparentError("That would create a circular parent chain.")
        pin.parent_pin = new_parent
    pin.save(update_fields=["parent_pin", "updated"])


def _promote_children(instance: Pin) -> list[int]:
    """Re-parent *instance*'s direct children ahead of its deletion.

    Children move up to the deleted pin's own parent; when the deleted pin
    was top-level they become top-level pins themselves. A child whose
    Location already has another top-level pin nests under that pin instead
    (top-level pins are unique per Location+profile).

    Children that would collide with *instance*'s own root slot (same
    Location) can only become top-level once the pin is actually gone, so
    they are temporarily self-parented (detaching them from the doomed
    cascade) and returned for :func:`_finish_deferred_promotions`.

    Args:
        instance: The pin about to be deleted.

    Returns:
        Primary keys of children whose promotion must finish post-delete.
    """
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

    Runs after the parent pin's row is gone, so its root slot is free. If
    several deferred children share one Location, the first becomes the
    top-level pin and the rest nest under it.

    Args:
        profile_id: Owner of the pins (root uniqueness is per profile).
        deferred_ids: Primary keys of the temporarily self-parented children.
    """
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

    A pin with descendants requires an explicit ``children_mode``:
    ``"delete"`` removes the whole subtree (the pins and their photos
    restorable from Undo History; CASCADEd content - comments, albums, links -
    is not, see ``PinUndoHandler``); ``"keep"`` promotes the direct children to the deleted pin's
    own parent (or to top-level pins) and deletes only the pin itself. Either
    way, every pin actually deleted is staged for undo and (via
    ``models.pin.signals``) gets a durable tombstone for sync clients.

    Args:
        pin: The pin to delete.
        children_mode: ``"delete"``, ``"keep"``, or ``""`` when the pin is
            known to have no descendants.

    Returns:
        The pins actually deleted (just ``pin`` in "keep" mode, the whole
        subtree otherwise).

    Raises:
        PinHasChildrenError: *pin* has descendants and ``children_mode`` is
            neither ``"delete"`` nor ``"keep"``.
    """
    subtree = list(Pin.objects.filter(pk=pin.pk).with_descendants())
    descendant_count = len(subtree) - 1

    if descendant_count and children_mode not in {"delete", "keep"}:
        raise PinHasChildrenError(descendant_count)

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
