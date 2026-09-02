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
    from django.db import transaction

    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.models.trips.model import Trip, TripMembership
    from urbanlens.dashboard.services.trips.trip_names import random_trip_name

    # Lock the profile row for the duration of the check-then-create so two
    # concurrent requests from the same user can't both pass the upcoming-trip
    # count check and jointly exceed the site's max_upcoming_trips_per_user.
    with transaction.atomic():
        ProfileModel.objects.select_for_update().get(pk=context.profile.pk)

        max_upcoming = SiteSettings.get_current().max_upcoming_trips_per_user
        if max_upcoming > 0 and Trip.objects.upcoming(context.profile).count() >= max_upcoming:
            return {"error": f"The user already has the maximum of {max_upcoming} upcoming trips."}

        name = args.name.strip() or random_trip_name()
        description = args.description.strip() or None
        trip = Trip.objects.create(name=name, description=description, creator=context.profile)
        TripMembership.objects.get_or_create(trip=trip, profile=context.profile, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED})
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
    ),
)


class AddTripActivityArgs(BaseModel):
    trip_slug: str = Field(max_length=255)
    pin_slug: str = Field(max_length=255)
    scheduled_date: str = Field(default="", max_length=32)


def _add_trip_activity(context: ToolContext, args: AddTripActivityArgs) -> dict[str, Any]:
    from django.db import transaction

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.models.trips.model import Trip, TripActivity
    from urbanlens.dashboard.services.trips.trip_share_tracking import record_trip_activity_shares

    trip = Trip.objects.filter(slug=args.trip_slug, profiles=context.profile).first()
    if trip is None:
        return {"error": "No such trip (it must be one of the user's own trips)."}
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

    # Lock the trip row for the duration of the check-then-create so two concurrent
    # requests (e.g. two members adding activities to the same trip at once, or the
    # user double-submitting) can't both pass the max_trip_activities count check
    # and jointly exceed it - same shape/reason as _create_trip's profile-row lock
    # above. Locked on the trip (not the profile) since the count this guards is
    # per-trip and other members can add activities to it too.
    with transaction.atomic():
        Trip.objects.select_for_update().get(pk=trip.pk)

        max_activities = SiteSettings.get_current().max_trip_activities
        if max_activities > 0 and trip.activities.count() >= max_activities:
            return {"error": f"That trip already has the maximum of {max_activities} activities."}

        activity = TripActivity.objects.create(
            trip=trip,
            pin=pin,
            location=pin.location,
            added_by=context.profile,
            title=None,
            scheduled_at=scheduled_at,
            order=trip.activities.count(),
            status=TripActivity.STATUS_PROPOSED,
        )
    # Same rule as the trip view: putting a place on an itinerary reveals it
    # to every member and must count in the sharer's reshare chain.
    record_trip_activity_shares(activity)
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
    ),
)
