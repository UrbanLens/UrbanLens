"""Undo handler for Trip (plus its membership/RSVP roster)."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.undo.base import UndoHandler, register

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


@register
class TripUndoHandler(UndoHandler):
    """Restores a trip's own fields and its membership/RSVP roster.

    Activities and comments cascade-delete with the trip before this handler
    gets a chance to capture them, and are not restored.
    """

    model_label = "trip"

    @classmethod
    def serialize(cls, instances: list[Trip]) -> list[dict[str, Any]]:
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
    def describe(cls, instances: list[Trip]) -> str:
        if len(instances) == 1:
            return f"Trip: {instances[0].name}"
        return f"{len(instances)} trips"

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Trip]:
        restored: list[Trip] = []
        for entry in payload:
            trip = Trip.objects.create(creator_id=entry["creator_id"], **entry["fields"])
            for membership_entry in entry["memberships"]:
                TripMembership.objects.create(trip=trip, **membership_entry)
            restored.append(trip)
        return restored
