"""Trip tools: list (read-only), create and add-activity (writes, confirm-gated).

Trips are multi-profile (``TripMembership``), so ``list_trips`` can
legitimately surface a trip someone else created and shared with the
requesting profile - :attr:`~registry.DataScope.VISIBLE_SHARED`, not
``OWN_PROFILE``, even though every query below is still scoped to
``context.profile`` and nothing here bypasses that membership check.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register

#: Rows any single trip tool may return.
_ROW_LIMIT = 10


class ListTripsArgs(BaseModel):
    pass


def _list_trips(context: ToolContext, args: ListTripsArgs) -> dict[str, Any]:
    from django.db.models import Count

    from urbanlens.dashboard.models.trips.model import Trip

    trips = Trip.objects.upcoming(context.profile).annotate(activity_count=Count("activities", distinct=True))[:_ROW_LIMIT]
    return {
        "trips": [
            {
                "name": trip.name,
                "slug": trip.slug,
                "start_date": trip.start_date.isoformat() if trip.start_date else None,
                "end_date": trip.end_date.isoformat() if trip.end_date else None,
                "activities": trip.activity_count,
            }
            for trip in trips
        ],
    }


register(
    ToolSpec(
        name="list_trips",
        description="List the requesting user's upcoming trips (including ones shared with them).",
        args_model=ListTripsArgs,
        handler=_list_trips,
        features=frozenset({SiteFeature.AI}),
        user_content_fields=frozenset({"name"}),
        scope=DataScope.VISIBLE_SHARED,
        progress_label="Checking your trips…",
        action_label="Checked your trips",
    ),
)


class CreateTripArgs(BaseModel):
    name: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=1000)


def _create_trip(context: ToolContext, args: CreateTripArgs) -> dict[str, Any]:
    # The shared service every other caller uses. It owns the name generation,
    # the membership join, the description length limit, and the
    # max_upcoming_trips_per_user quota under a lock on the creator's profile
    # row - the lock this tool used to hold on its own.
    from urbanlens.dashboard.services.trips.trip_crud import create_trip
    from urbanlens.dashboard.services.trips.trip_errors import TripError

    try:
        trip, _created = create_trip(context.profile, name=args.name, description=args.description)
    except TripError as exc:
        return {"error": str(exc)}
    return {"created": {"name": trip.name, "slug": trip.slug}}


register(
    ToolSpec(
        name="create_trip",
        description="Create a new trip for the requesting user.",
        args_model=CreateTripArgs,
        handler=_create_trip,
        read_only=False,
        requires_confirmation=True,
        features=frozenset({SiteFeature.AI}),
        user_content_fields=frozenset({"name"}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Creating a trip…",
        action_label="Created a trip",
        confirm_label="Create trip",
    ),
)


class AddTripActivityArgs(BaseModel):
    trip_slug: str = Field(max_length=255)
    pin_slug: str = Field(max_length=255)
    scheduled_date: str = Field(default="", max_length=32)


def _add_trip_activity(context: ToolContext, args: AddTripActivityArgs) -> dict[str, Any]:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.services.trips.trip_activities import create_activity
    from urbanlens.dashboard.services.trips.trip_errors import TripError

    trip = Trip.objects.filter(slug=args.trip_slug, profiles=context.profile).first()
    if trip is None:
        return {"error": "No such trip (it must be one of the user's own trips)."}
    # Membership only narrows *which* trip; whether this profile may add to it is
    # `create_activity`'s call, via allow_add_activities and joined-ness. The
    # filter above matches through TripMembership with no status filter, so it
    # includes invited-not-joined members too.
    pin = Pin.objects.filter(slug=args.pin_slug, profile=context.profile, parent_pin__isnull=True).select_related("location").first()
    if pin is None:
        return {"error": "No such pin (it must be one of the user's own pins)."}

    scheduled_at = None
    raw_date = args.scheduled_date.strip()
    if raw_date:
        from datetime import datetime, time

        from django.utils.dateparse import parse_date
        from django.utils.timezone import get_current_timezone

        day = parse_date(raw_date)
        if day is not None:
            # 9am local: an arbitrary-but-sane default hour for a date-only plan.
            scheduled_at = datetime.combine(day, time(hour=9), tzinfo=get_current_timezone())

    # The shared service every other caller uses. It owns the permission check
    # (allow_add_activities, and joined-ness), the max_trip_activities quota
    # under a trip-row lock, the append position, and the reshare-chain record -
    # all of which this tool used to re-implement, and two of which it had never
    # implemented at all.
    try:
        activity = create_activity(trip, context.profile, place={"pin_slug": args.pin_slug}, scheduled_at=scheduled_at)
    except TripError as exc:
        return {"error": str(exc)}
    return {"added": {"trip": trip.name, "pin": pin.effective_name, "activity_id": activity.id}}


register(
    ToolSpec(
        name="add_trip_activity",
        description="Add one of the requesting user's own pins to one of their trips as a proposed activity.",
        args_model=AddTripActivityArgs,
        handler=_add_trip_activity,
        read_only=False,
        requires_confirmation=True,
        features=frozenset({SiteFeature.AI}),
        user_content_fields=frozenset({"trip", "pin"}),
        scope=DataScope.VISIBLE_SHARED,
        progress_label="Adding a pin to a trip…",
        action_label="Added a pin to a trip",
        confirm_label="Add to trip",
    ),
)
