"""Undo handler for Pin (root pins and their personal detail-pin subtree)."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.base import UndoHandler, register

# Fields restored verbatim on undo. Deliberately excludes uuid/slug/created/updated
# (regenerated fresh by Pin.save()) and the location/profile/wiki/parent_pin FKs
# (handled separately below, since FK columns need their `_id` attname, not the
# relation name, to be passed to Pin.objects.create()).
_RESTORABLE_FIELDS = (
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


@register
class PinUndoHandler(UndoHandler):
    """Restores a pin's own fields, hierarchy position, and badges - not its cascade children.

    Reviews, visit history, notes, markup annotations, aliases, and comments
    are gone the instant the pin is deleted and are not restored.
    """

    model_label = "pin"

    @classmethod
    def serialize(cls, instances: list[Pin]) -> list[dict[str, Any]]:
        return [cls._serialize_one(pin) for pin in instances]

    @classmethod
    def _serialize_one(cls, pin: Pin) -> dict[str, Any]:
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

    @classmethod
    def describe(cls, instances: list[Pin]) -> str:
        if len(instances) == 1:
            return f"Pin: {instances[0].effective_name}"
        return f"{len(instances)} pins"

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Pin]:
        """Recreate pins with fresh pks/uuids/slugs, relinking hierarchy and badges.

        Parent/child relationships within the restored batch are relinked in
        a second pass once every pin has a new pk to relink against.
        """
        old_to_new: dict[int, Pin] = {}
        restored: list[Pin] = []
        for entry in payload:
            pin = Pin.objects.create(
                location_id=entry["location_id"],
                profile_id=entry["profile_id"],
                wiki_id=entry["wiki_id"],
                **entry["fields"],
            )
            old_to_new[entry["old_pk"]] = pin
            restored.append(pin)

        for entry, pin in zip(payload, restored, strict=True):
            old_parent_pk = entry["parent_pin_old_pk"]
            if old_parent_pk:
                if old_parent_pk in old_to_new:
                    pin.parent_pin = old_to_new[old_parent_pk]
                    pin.save(update_fields=["parent_pin"])
                else:
                    # The parent wasn't part of this deletion (a sub pin was
                    # deleted on its own) - reattach to it if it still exists.
                    surviving_parent = Pin.objects.filter(pk=old_parent_pk, profile_id=entry["profile_id"]).first()
                    if surviving_parent is not None:
                        pin.parent_pin = surviving_parent
                        pin.save(update_fields=["parent_pin"])
            if entry["badge_ids"]:
                pin.badges.set(entry["badge_ids"])

        return restored
