"""
A boundary the user draws on their own pin is a private grouping: their other top-level pins inside it nest under it.

Only the owner's own pins are ever read or changed, and the boundary is only ever read through the owner's profile.
A boundary never confers access to anything: this module touches ``Pin.parent_pin`` and nothing that wiki or place
access (``wiki_access``) looks at, and it never merges, dedups or changes a ``Location``.

Choices on the edges:

* A pin the user parented, promoted or detached by hand (``Pin.parenting_is_manual``) is never touched.
* A pin already nested under another pin by hand is not a candidate; only top-level pins and pins this module nested
  (``Pin.nested_by_boundary_pin``) are.
* Shrinking or deleting a boundary returns the pins it nested to the top level (unless that would collide with another
  top-level pin of the user at the same Location, in which case they stay nested). Pins the user arranged are untouched.
* Where several boundaries contain a pin it goes under the smallest, and a pin never nests under its own descendant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import Area
from django.db import transaction
from django.db.models import Q

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


def _owners_smallest_first(profile_id: int) -> list[tuple[int, object]]:
    """The profile's pins that carry a drawn polygon, as ``(pin_id, polygon)``, smallest boundary first."""
    from urbanlens.dashboard.models.boundary.model import Boundary

    rows = Boundary.objects.filter(pin__profile_id=profile_id, polygon__isnull=False).annotate(square_metres=Area("polygon")).order_by("square_metres", "pin_id").values_list("pin_id", "polygon", "square_metres")
    return [(pin_id, polygon) for pin_id, polygon, _area in rows]


def _wanted_parents(profile_id: int, candidates: dict[int, Pin]) -> dict[int, int]:
    """Candidate pin id to the owner whose smallest containing boundary holds it."""
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    wanted: dict[int, int] = {}
    for owner_id, polygon in _owners_smallest_first(profile_id):
        inside = PinModel.objects.filter(pk__in=candidates.keys(), location__point__intersects=polygon).exclude(pk=owner_id).values_list("pk", flat=True)
        for pin_id in inside:
            wanted.setdefault(pin_id, owner_id)
    return wanted


def nest_pins_for_profile(profile_id: int) -> int:
    """
    Bring one user's boundary-nested pins in line with their drawn boundaries.

    Idempotent: running it twice changes nothing the second time.

    Args:
        profile_id: The user whose pins to arrange. No other profile's pins or boundaries are read.

    Returns:
        How many pins were nested, moved or returned to the top level.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    changed = 0
    with transaction.atomic():
        # The profile's pins are locked so two triggers (a boundary save and an import) do not interleave.
        list(PinModel.objects.select_for_update(of=("self",)).filter(profile_id=profile_id).order_by("pk").values_list("pk", flat=True))
        candidates = {pin.pk: pin for pin in PinModel.objects.filter(profile_id=profile_id, parenting_is_manual=False).filter(Q(parent_pin__isnull=True) | Q(nested_by_boundary_pin__isnull=False))}
        wanted = _wanted_parents(profile_id, candidates)
        for pin in candidates.values():
            target_id = wanted.get(pin.pk)
            if target_id == pin.parent_pin_id and (target_id is None) == (pin.nested_by_boundary_pin_id is None):
                continue
            if target_id is None:
                if pin.nested_by_boundary_pin_id is None:
                    continue
                if PinModel.objects.root_pins().filter(profile_id=profile_id, location_id=pin.location_id).exclude(pk=pin.pk).exists():
                    continue
                new_parent = None
            else:
                new_parent = PinModel.objects.filter(pk=target_id, profile_id=profile_id).first()
                if new_parent is None or pin.would_create_cycle(new_parent):
                    continue
            pin.parent_pin = new_parent
            pin.nested_by_boundary_pin = new_parent
            pin.save(update_fields=["parent_pin", "nested_by_boundary_pin", "updated"])
            changed += 1
    if changed:
        logger.info("boundary_nesting: arranged %d pin(s) for profile %s", changed, profile_id)
    return changed


def request_nesting(profile_id: int) -> None:
    """
    Arrange a user's pins once the current transaction commits.

    Args:
        profile_id: The user whose pins to arrange.
    """

    def run() -> None:
        try:
            nest_pins_for_profile(profile_id)
        except Exception:
            # A failed grouping must never fail the save or import that asked for it.
            logger.exception("boundary_nesting: failed for profile %s", profile_id)

    transaction.on_commit(run)
