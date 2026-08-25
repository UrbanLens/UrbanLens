"""Undo handler for Trip (plus its membership/RSVP roster)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

_RESTORABLE_FIELDS = (
    "name",
    "description",
    "start_date",
    "end_date",
    "allow_add_members",
    "allow_add_activities",
    "allow_edit_activities",
    "allow_comments",
)

_MEMBERSHIP_FIELDS = ("rsvp", "is_organizer")

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.trip import MODEL_LABEL``) instead
#: of hand-typing ``"trip"`` - a typo in a hand-typed string only fails at
#: runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "trip"


@register
class TripUndoHandler(UndoHandler):
    """Restores a trip's own fields and its membership/RSVP roster.

    Activities and comments cascade-delete with the trip before this handler
    gets a chance to capture them, and are not restored.
    """

    model_label = MODEL_LABEL

    @classmethod
    def serialize(cls, instances: Sequence[Trip]) -> list[dict[str, Any]]:
        return [cls._serialize_one(trip) for trip in instances]

    @classmethod
    def _serialize_one(cls, trip: Trip) -> dict[str, Any]:
        fields = {name: getattr(trip, name) for name in _RESTORABLE_FIELDS}
        return {
            "fields": fields,
            "creator_id": trip.creator_id,
            "memberships": [
                {
                    **{name: getattr(membership, name) for name in _MEMBERSHIP_FIELDS},
                    "profile_id": membership.profile_id,
                }
                for membership in trip.memberships.all()
            ],
        }

    @classmethod
    def describe(cls, instances: Sequence[Trip]) -> str:
        return describe_batch("Trip", "trips", [t.name for t in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Trip]:
        """Recreate trips and their membership rosters.

        Raises:
            UndoExpiredError: If the creator, or any roster member's profile,
                was independently deleted during the retention window, since
                recreating the row would otherwise fail with an uncaught
                ``IntegrityError`` - both FKs are non-nullable-in-practice
                here (``creator_id`` is only ``SET_NULL`` for a *live* trip
                whose creator later deletes their account, never written as
                ``None`` by this handler) or genuinely non-nullable
                (``TripMembership.profile``).
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["creator_id"]).exists():
                raise UndoExpiredError("This trip's creator no longer exists.")
            member_ids = [membership_entry["profile_id"] for membership_entry in entry["memberships"]]
            if member_ids and Profile.objects.filter(pk__in=member_ids).count() != len(set(member_ids)):
                raise UndoExpiredError("One of this trip's members no longer exists.")

        restored: list[Trip] = []
        for entry in payload:
            trip = Trip.objects.create(creator_id=entry["creator_id"], **entry["fields"])
            for membership_entry in entry["memberships"]:
                TripMembership.objects.create(trip=trip, **membership_entry)
            restored.append(trip)
        return restored
