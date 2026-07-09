"""Cache-backed staging so a bulk pin delete can be undone for a short window.

Deleting a Pin cascades to reviews, visit history, notes, markup annotations,
aliases, comments, and the pin's own campus boundary override (all
``on_delete=CASCADE`` from the DB's point of view) - those rows are gone the
instant the delete happens, before anything here gets a chance to cache them.
So restoring only brings back each pin's own core fields, its position in the
``parent_pin`` hierarchy, and its badges - callers must surface that scope
limit to the user before they confirm the delete.
"""

from __future__ import annotations

import logging
from typing import Any
import uuid

from django.core.cache import cache

from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

UNDO_TTL_SECONDS = 300

# Fields restored verbatim on undo. Deliberately excludes uuid/slug/created/updated
# (regenerated fresh by Pin.save()) and the location/profile/wiki/parent_pin FKs
# (handled separately below, since FK columns need their `_id` attname, not the
# relation name, to be passed to Pin.objects.create()).
_RESTORABLE_FIELDS = (
    "is_private",
    "name_is_user_provided",
    "name",
    "icon",
    "description",
    "priority",
    "vulnerability",
    "danger",
    "last_visited",
    "unlogged_visit_dismissed",
    "pin_type",
    "color",
    "detail_bg_color",
    "detail_bg_opacity",
    "detail_border_color",
    "detail_border_opacity",
    "date_abandoned",
    "date_last_active",
    "fences",
    "alarms",
    "cameras",
    "security",
    "signs",
    "vps",
    "plywood",
    "locked",
)


def _cache_key(token: str) -> str:
    return f"dashboard:pin_undo:{token}"


def _serialize_pin(pin: Pin) -> dict[str, Any]:
    """Capture one pin's restorable fields, keyed by its current pk for relinking."""
    fields = {name: getattr(pin, name) for name in _RESTORABLE_FIELDS}
    fields["custom_icon"] = pin.custom_icon.name if pin.custom_icon else None
    return {
        "old_pk": pin.pk,
        "fields": fields,
        "location_id": pin.location_id,
        "profile_id": pin.profile_id,
        "wiki_id": pin.wiki_id,
        "parent_pin_old_pk": pin.parent_pin_id,
        "badge_ids": list(pin.badges.values_list("id", flat=True)),
    }


def stash_pins_for_undo(pins: list[Pin], profile_id: int) -> str:
    """Serialize pins into cache so they can be recreated within a short grace period.

    Must be called before the pins are deleted. Callers are responsible for
    including a pin's full descendant subtree (e.g. via
    ``Pin.objects.with_descendants()``) since deleting a pin cascades to its
    children.

    Args:
        pins: The pins to stash.
        profile_id: Owning profile, re-checked on restore.

    Returns:
        A token identifying this stash, to pass to ``restore_pins_from_undo``.
    """
    token = str(uuid.uuid4())
    payload = {
        "profile_id": profile_id,
        "pins": [_serialize_pin(pin) for pin in pins],
    }
    cache.set(_cache_key(token), payload, timeout=UNDO_TTL_SECONDS)
    return token


def restore_pins_from_undo(token: str, profile_id: int) -> list[Pin] | None:
    """Recreate pins previously stashed by ``stash_pins_for_undo``.

    Recreated pins get fresh primary keys, uuids, and slugs. Parent/child
    relationships within the restored batch are relinked in a second pass
    once every pin has a new pk to relink against.

    Args:
        token: The token returned by ``stash_pins_for_undo``.
        profile_id: Must match the profile the pins were stashed for.

    Returns:
        The recreated pins, or None if the token is missing, expired, or
        belongs to a different profile.
    """
    key = _cache_key(token)
    payload = cache.get(key)
    if not payload or payload.get("profile_id") != profile_id:
        return None

    old_to_new: dict[int, Pin] = {}
    restored: list[Pin] = []
    for entry in payload["pins"]:
        pin = Pin.objects.create(
            location_id=entry["location_id"],
            profile_id=entry["profile_id"],
            wiki_id=entry["wiki_id"],
            **entry["fields"],
        )
        old_to_new[entry["old_pk"]] = pin
        restored.append(pin)

    for entry, pin in zip(payload["pins"], restored, strict=True):
        old_parent_pk = entry["parent_pin_old_pk"]
        if old_parent_pk and old_parent_pk in old_to_new:
            pin.parent_pin = old_to_new[old_parent_pk]
            pin.save(update_fields=["parent_pin"])
        if entry["badge_ids"]:
            pin.badges.set(entry["badge_ids"])

    cache.delete(key)
    return restored
