"""Shared pin move/reparent/delete logic.

This is the single code path behind moving, reparenting, and deleting a Pin,
shared by the internal DRF surface (``models.pin.viewset.PinViewSet``) and the
external API (``external_api.views.PinDetailView``) - mirrors the same
one-implementation principle as ``services.pin_creation`` for pin creation.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.handlers.pin import MODEL_LABEL as PIN_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo

logger = logging.getLogger(__name__)


class PinReparentError(ValueError):
    """The requested parent change is invalid.

    The message is safe to surface directly to the caller.
    """


class PinHasChildrenError(ValueError):
    """A delete was requested without saying what to do with the pin's children.

    Callers should ask the user, then retry with an explicit ``children_mode``.
    """

    def __init__(self, descendant_count: int) -> None:
        self.descendant_count = descendant_count
        super().__init__("This pin has child pins - specify children_mode='delete' or 'keep'.")


def move_pin_to_coordinates(pin: Pin, latitude: float, longitude: float) -> None:
    """Repoint *pin* to a new/existing Location at the given coordinates.

    Coordinates live on ``Location`` (not ``Pin``), so this repoints
    ``pin.location`` rather than writing through a serializer. Uses
    ``threshold_meters=0`` so a manual move always lands on the exact
    submitted point rather than snapping to whatever Location happens to
    already exist within the default dedup radius (mirrors
    ``pin_creation.resolve_child_pin_location``).

    Args:
        pin: The pin being moved.
        latitude: New latitude, already validated to be in range.
        longitude: New longitude, already validated to be in range.
    """
    from urbanlens.dashboard.models.location.model import Location

    location, _created = Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=0)
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
    ``"delete"`` removes the whole subtree (all of it restorable from Undo
    History); ``"keep"`` promotes the direct children to the deleted pin's
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
