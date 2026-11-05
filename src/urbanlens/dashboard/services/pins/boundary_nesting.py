"""
A boundary the user draws on their own pin is a private grouping: their other top-level pins inside it nest under it.

Only the owner's own pins are ever read or changed, and the boundary is only ever read through the owner's profile.
A boundary never confers access to anything: this module touches ``Pin.parent_pin`` and nothing that wiki or place
access (``wiki_access``) looks at, and it never merges, dedups or changes a ``Location``.

Choices on the edges:

* A pin the user parented, promoted or detached by hand (``Pin.parenting_is_manual``) is never touched.
* A pin already nested under another pin by hand is not a candidate; only top-level pins and pins this module nested
  (``Pin.nested_by_boundary_pin``) are.
* Every pass is scoped to its event: one pin that was created or moved, or the pins in a changed boundary's old and
  new outline plus those it had nested. Nothing walks the whole profile.
* Shrinking or deleting a boundary returns the pins it nested to the top level (unless that would collide with another
  top-level pin of the user at the same Location, in which case they stay nested). Pins the user arranged are untouched.
* Where several boundaries contain a pin it goes under the smallest, and a pin never nests under its own descendant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import Area
from django.db import connection, transaction
from django.db.models import Q

from urbanlens.dashboard.services.pins.tree_lock import lock_profile_pin_tree

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


def _containing_owners(profile_id: int, pin_id: int, point) -> list[int]:
    """Pins of the profile whose drawn boundary holds *point*, smallest boundary first (ties by pk)."""
    from urbanlens.dashboard.models.boundary.model import Boundary

    owners: list[int] = []
    rows = Boundary.objects.filter(pin__profile_id=profile_id, polygon__isnull=False, polygon__intersects=point).exclude(pin_id=pin_id).annotate(square_metres=Area("polygon")).order_by("square_metres", "pin_id").values_list("pin_id", flat=True)
    for owner_id in rows:
        if owner_id not in owners:
            owners.append(owner_id)
    return owners


def _settle(pin_id: int) -> bool:
    """
    Put one pin under the smallest of its owner's boundaries that holds it, or back at the top level.

    Call with the profile's pin-tree lock held. The pin and its target parent are row-locked in pk order and the cycle
    check is repeated under those locks.

    Returns:
        Whether the pin's parent changed.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    pin = PinModel.objects.select_related("location").filter(pk=pin_id).first()
    if pin is None or pin.parenting_is_manual or (pin.parent_pin_id is not None and pin.nested_by_boundary_pin_id is None):
        return False
    owner_ids = _containing_owners(pin.profile_id, pin.pk, pin.location.point)
    row_ids = sorted({pin.pk, *owner_ids})
    locked = {row.pk: row for row in PinModel.objects.select_for_update(of=("self",)).select_related("location").filter(pk__in=row_ids, profile_id=pin.profile_id).order_by("pk")}
    pin = locked.get(pin.pk)
    if pin is None or pin.parenting_is_manual or (pin.parent_pin_id is not None and pin.nested_by_boundary_pin_id is None):
        return False
    new_parent = None
    for owner_id in owner_ids:
        owner = locked.get(owner_id)
        if owner is not None and not pin.would_create_cycle(owner):
            new_parent = owner
            break
    if new_parent is None:
        if pin.nested_by_boundary_pin_id is None:
            return False
        if PinModel.objects.root_pins().filter(profile_id=pin.profile_id, location_id=pin.location_id).exclude(pk=pin.pk).exists():
            return False
    elif pin.parent_pin_id == new_parent.pk and pin.nested_by_boundary_pin_id == new_parent.pk:
        return False
    pin.parent_pin = new_parent
    pin.nested_by_boundary_pin = new_parent
    pin.save(update_fields=["parent_pin", "nested_by_boundary_pin", "updated"])
    return True


def _has_drawn_boundary(profile_id: int) -> bool:
    from urbanlens.dashboard.models.boundary.model import Boundary

    return Boundary.objects.filter(pin__profile_id=profile_id, polygon__isnull=False).exists()


def nest_pins(profile_id: int, pin_ids: Iterable[int]) -> int:
    """
    Settle pins that were just created or moved.

    Args:
        profile_id: Their owner. No other profile's pins or boundaries are read.
        pin_ids: The pins to test, each against only the owner's drawn boundaries.

    Returns:
        How many pins changed parent.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    ids = sorted(set(pin_ids))
    if ids and not _has_drawn_boundary(profile_id):
        # With no outline left, only a pin a boundary nested still needs releasing.
        ids = sorted(PinModel.objects.filter(pk__in=ids, profile_id=profile_id, nested_by_boundary_pin__isnull=False).values_list("pk", flat=True))
    if not ids:
        return 0
    changed = 0
    with transaction.atomic():
        lock_profile_pin_tree(profile_id)
        for pin_id in ids:
            changed += _settle(pin_id)
    return changed


def renest_for_boundary(profile_id: int, owner_id: int, old_polygon, new_polygon) -> int:
    """
    Re-settle the pins a drawn boundary's change can affect.

    Args:
        profile_id: The boundary owner's profile.
        owner_id: The pin the boundary belongs to.
        old_polygon: Its previous outline, or None.
        new_polygon: Its outline after the change, or None when cleared.

    Returns:
        How many pins changed parent.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    reach = Q(nested_by_boundary_pin_id=owner_id)
    for polygon in (old_polygon, new_polygon):
        if polygon is not None:
            reach |= Q(location__point__intersects=polygon)
    candidates = PinModel.objects.filter(profile_id=profile_id, parenting_is_manual=False).filter(Q(parent_pin__isnull=True) | Q(nested_by_boundary_pin__isnull=False)).filter(reach).exclude(pk=owner_id)
    ids = list(candidates.order_by("pk").values_list("pk", flat=True))
    if not ids:
        return 0
    changed = 0
    with transaction.atomic():
        lock_profile_pin_tree(profile_id)
        for pin_id in ids:
            changed += _settle(pin_id)
    return changed


class _Pass:
    """One deferred nesting pass; equal passes queued in the same transaction run once."""

    def __init__(self, profile_id: int, owner_id: int | None = None, old_polygon=None, new_polygon=None) -> None:
        self.profile_id = profile_id
        self.owner_id = owner_id
        self.old_polygon = old_polygon
        self.new_polygon = new_polygon
        self.pin_ids: set[int] = set()
        self.ran = False

    def same_event(self, other: _Pass) -> bool:
        if self.owner_id is None or other.owner_id is None:
            return self.owner_id is other.owner_id and self.profile_id == other.profile_id
        return (self.profile_id, self.owner_id) == (other.profile_id, other.owner_id) and self.old_polygon == other.old_polygon and self.new_polygon == other.new_polygon

    def __call__(self) -> None:
        self.ran = True
        try:
            if self.owner_id is None:
                nest_pins(self.profile_id, self.pin_ids)
            else:
                renest_for_boundary(self.profile_id, self.owner_id, self.old_polygon, self.new_polygon)
        except Exception:
            # A failed grouping must never fail the save or import that asked for it.
            logger.exception("boundary_nesting: failed for profile %s", self.profile_id)


def _queue(event: _Pass) -> _Pass:
    """Queue *event* after commit, or fold it into an equal one already queued in this transaction."""
    for _sids, queued, *_ in connection.run_on_commit:
        if isinstance(queued, _Pass) and not queued.ran and queued.same_event(event):
            return queued
    transaction.on_commit(event)
    return event


def request_pin_nesting(profile_id: int, pin_id: int) -> None:
    """Settle a created or moved pin after commit; every pin of one profile queued in a transaction shares a pass."""
    event = _Pass(profile_id)
    event.pin_ids.add(pin_id)
    queued = _queue(event)
    if queued is not event:
        queued.pin_ids.add(pin_id)


def request_boundary_nesting(profile_id: int, owner_id: int, old_polygon, new_polygon) -> None:
    """Re-settle the pins a drawn boundary's change affects, after commit."""
    _queue(_Pass(profile_id, owner_id, old_polygon, new_polygon))
