"""Undo handler for SafetyCheckin (plus its emergency-contact snapshots)."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
from urbanlens.dashboard.services.undo.base import UndoHandler, register

_RESTORABLE_FIELDS = (
    "title",
    "plan_details",
    "contact_message",
    "checkin_by",
    "grace_period",
    "status",
    "destination_latitude",
    "destination_longitude",
    "reminder_sent_at",
    "final_warning_sent_at",
    "escalated_at",
    "resolved_at",
    "plan_update_notified_at",
    "notify_community_wiki",
    "wiki_notified_at",
)

_CONTACT_FIELDS = ("email", "name", "notified_at", "found_safe_at")


@register
class SafetyCheckinUndoHandler(UndoHandler):
    """Restores a check-in's own fields and its emergency-contact snapshots.

    Chat messages and per-contact opt-outs are conversational/audit history,
    not state needed to resume the check-in, and are not restored. The
    attached ``markup_map``/``markup_maps`` survive independently of the
    check-in (deleting a SafetyCheckin does not cascade to its maps), so
    those are simply relinked by id.
    """

    model_label = "safety_checkin"

    @classmethod
    def serialize(cls, instances: list[SafetyCheckin]) -> list[dict[str, Any]]:
        return [cls._serialize_one(checkin) for checkin in instances]

    @classmethod
    def _serialize_one(cls, checkin: SafetyCheckin) -> dict[str, Any]:
        fields = {name: getattr(checkin, name) for name in _RESTORABLE_FIELDS}
        return {
            "fields": fields,
            "profile_id": checkin.profile_id,
            "destination_location_id": checkin.destination_location_id,
            "markup_map_id": checkin.markup_map_id,
            "markup_map_ids": list(checkin.markup_maps.values_list("id", flat=True)),
            "contacts": [
                {
                    **{name: getattr(contact, name) for name in _CONTACT_FIELDS},
                    "contact_profile_id": contact.contact_profile_id,
                }
                for contact in checkin.contacts.all()
            ],
        }

    @classmethod
    def describe(cls, instances: list[SafetyCheckin]) -> str:
        if len(instances) == 1:
            return f"Safety check-in: {instances[0].title}"
        return f"{len(instances)} safety check-ins"

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[SafetyCheckin]:
        restored: list[SafetyCheckin] = []
        for entry in payload:
            checkin = SafetyCheckin.objects.create(
                profile_id=entry["profile_id"],
                destination_location_id=entry["destination_location_id"],
                markup_map_id=entry["markup_map_id"],
                **entry["fields"],
            )
            if entry["markup_map_ids"]:
                checkin.markup_maps.set(entry["markup_map_ids"])
            for contact_entry in entry["contacts"]:
                SafetyCheckinContact.objects.create(checkin=checkin, **contact_entry)
            restored.append(checkin)
        return restored
