"""Undo handler for SafetyCheckin (plus its emergency-contact snapshots)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinPartner
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    "live_location_sharing_enabled",
    "live_latitude",
    "live_longitude",
    "live_location_accuracy",
    "live_location_updated_at",
    "archive_scheduled_at",
    "resolved_by_label",
)

_CONTACT_FIELDS = ("email", "name", "notified_at", "found_safe_at")

_PARTNER_FIELDS = ("status", "accepted_at")


#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.safety_checkin import MODEL_LABEL``)
#: instead of hand-typing ``"safety_checkin"`` - a typo in a hand-typed string
#: only fails at runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "safety_checkin"


@register
class SafetyCheckinUndoHandler(UndoHandler):
    """Restores a check-in's own fields, its emergency-contact snapshots, and its partners.

    Chat messages and per-contact opt-outs are conversational/audit history,
    not state needed to resume the check-in, and are not restored. A partner
    assignment, unlike chat history, *is* state needed to resume the
    check-in - so it's restored the same way contacts are. The attached
    ``markup_map``/``markup_maps`` survive independently of the check-in
    (deleting a SafetyCheckin does not cascade to its maps), so those are
    simply relinked by id.

    ``SafetyCheckinArchive`` (the encrypted post-resolution remnant) is
    deliberately not restored as its own row: a check-in that reached
    archival already has its plaintext PII scrubbed on the very row being
    serialized here, so restoring that row naturally reproduces the
    already-scrubbed state. If ``archive_scheduled_at`` is still in the past
    on a restored row, the periodic archival sweep
    (``tasks.sweep_due_safety_checkin_archival``) picks it up within 5
    minutes on its own - no explicit re-scheduling needed here.
    """

    model_label = MODEL_LABEL
    model = SafetyCheckin

    @classmethod
    def serialize(cls, instances: Sequence[SafetyCheckin]) -> list[dict[str, Any]]:
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
            # Image.safety_checkin is SET_NULL, so the delete detached these photos rather
            # than destroying them, and nothing else records where they were.
            "image_ids": list(checkin.images.values_list("pk", flat=True)),
            "contacts": [
                {
                    **{name: getattr(contact, name) for name in _CONTACT_FIELDS},
                    "contact_profile_id": contact.contact_profile_id,
                }
                for contact in checkin.contacts.all()
            ],
            "partners": [
                {
                    **{name: getattr(partner, name) for name in _PARTNER_FIELDS},
                    "profile_id": partner.profile_id,
                    "invited_by_id": partner.invited_by_id,
                }
                for partner in checkin.partners.all()
            ],
        }

    @classmethod
    def describe(cls, instances: Sequence[SafetyCheckin]) -> str:
        return describe_batch("Safety check-in", "safety check-ins", [c.title for c in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[SafetyCheckin]:
        """Recreate check-ins, their contact snapshots, and their partners.

        Raises:
            UndoExpiredError: If the owning profile, destination location,
                attached markup map(s), a contact's linked profile, or a
                partner's profile/inviter was independently deleted during
                the retention window, since recreating the row would
                otherwise fail with an uncaught IntegrityError.
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this check-in no longer exists.")

            destination_location_id = entry["destination_location_id"]
            if destination_location_id is not None and not Location.objects.filter(pk=destination_location_id).exists():
                raise UndoExpiredError("The destination for this check-in no longer exists.")

            markup_map_id = entry["markup_map_id"]
            if markup_map_id is not None and not MarkupMap.objects.filter(pk=markup_map_id).exists():
                raise UndoExpiredError("The route map for this check-in no longer exists.")

            markup_map_ids = entry["markup_map_ids"]
            if markup_map_ids and MarkupMap.objects.filter(pk__in=markup_map_ids).count() != len(set(markup_map_ids)):
                raise UndoExpiredError("One of the maps attached to this check-in no longer exists.")

            for contact_entry in entry["contacts"]:
                contact_profile_id = contact_entry["contact_profile_id"]
                if contact_profile_id is not None and not Profile.objects.filter(pk=contact_profile_id).exists():
                    raise UndoExpiredError("One of the people/labels involved in this check-in no longer exists.")

            # .get(): payloads stashed before partners existed have no "partners" key.
            for partner_entry in entry.get("partners", []):
                partner_profile_ids = (partner_entry["profile_id"], partner_entry["invited_by_id"])
                if Profile.objects.filter(pk__in=partner_profile_ids).count() != len(set(partner_profile_ids)):
                    raise UndoExpiredError("One of the people/labels involved in this check-in no longer exists.")

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
            for partner_entry in entry.get("partners", []):
                SafetyCheckinPartner.objects.create(checkin=checkin, **partner_entry)
            image_ids = entry.get("image_ids") or []
            if image_ids:
                # Same rule as the pin and wiki handlers: re-link only what is
                # still detached.
                Image.objects.filter(pk__in=image_ids, safety_checkin__isnull=True).update(safety_checkin=checkin)
            restored.append(checkin)
        return restored
