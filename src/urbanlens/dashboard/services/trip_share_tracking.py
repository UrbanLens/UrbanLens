"""Share-chain tracking for places revealed through trip activities.

Adding a pin/location to a trip as an activity reveals that place to every
other member of the trip - which is a share, and must count in the sharer's
reshare chain exactly like an explicit pin share (see
``services.share_provenance``). Two symmetric entry points:

- :func:`record_trip_activity_shares` - a new activity was added; record a
  detected share to every member who has already joined.
- :func:`record_trip_shares_for_member` - a profile joined the trip; record a
  detected share for every place already on the itinerary.

Both create ``PinShare`` rows with ``origin=TRIP_ACTIVITY`` and
``status=DETECTED`` (never actionable, never materializes a Pin), then record
the recipient's ``LocationExposure`` so future pins they drop at the place
chain back correctly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import DatabaseError

from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.share_provenance import (
    find_profile_pin_near_location,
    profile_is_exposed_to,
    record_share_exposure,
    resolve_and_stamp_origin_share,
    resolve_origin_share,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip, TripActivity

logger = logging.getLogger(__name__)


def _activity_place(activity: TripActivity) -> tuple[Pin | None, Location | None]:
    """The (pin, location) pair an activity reveals, or ``(None, None)``.

    Hidden-location activities ("Secret Location") reveal nothing.

    Args:
        activity: The activity to inspect.

    Returns:
        The pin (when linked) and the effective Location.
    """
    if activity.location_hidden:
        return None, None
    pin = activity.pin
    location = activity.location or (pin.location if pin is not None else None)
    return pin, location


def _record_detected_trip_share(sharer: Profile, recipient: Profile, pin: Pin | None, location: Location) -> PinShare | None:
    """Create one TRIP_ACTIVITY detected share, applying the shared dedup rules.

    Skipped when it wouldn't be the recipient's initial information about the
    place: they already have their own pin there, they already carry an
    exposure for it, or a share of this exact pin already reached them.

    Args:
        sharer: The member who put the place on the itinerary.
        recipient: The member the place is being revealed to.
        pin: The sharer's pin, when the activity links one.
        location: The place's Location.

    Returns:
        The newly created share, or None when skipped.
    """
    if recipient.pk == sharer.pk:
        return None
    # An activity may link a pin the sharer doesn't own (e.g. re-linked after
    # a membership change) - never stamp lineage onto someone else's pin.
    if pin is not None and pin.profile_id != sharer.pk:
        pin = None
    if find_profile_pin_near_location(recipient.pk, location) is not None:
        return None
    if profile_is_exposed_to(recipient.pk, location):
        return None
    if pin is not None and PinShare.objects.filter(pin=pin, to_profile=recipient).exists():
        return None
    if pin is None and PinShare.objects.filter(location=location, to_profile=recipient).exists():
        return None

    parent = resolve_and_stamp_origin_share(pin) if pin is not None else resolve_origin_share(sharer.pk, location=location)
    try:
        share = PinShare.objects.create(
            pin=pin,
            location=location,
            from_profile=sharer,
            to_profile=recipient,
            parent_share=parent,
            origin=PinShareOrigin.TRIP_ACTIVITY,
            status=PinShareStatus.DETECTED,
        )
    except DatabaseError:
        logger.exception("Could not record trip-activity share of location %s to profile %s", location.pk, recipient.pk)
        return None
    record_share_exposure(share)
    return share


def _joined_member_profiles(trip: Trip) -> list[Profile]:
    """Every profile that has joined the trip, including its creator."""
    from urbanlens.dashboard.models.trips.model import TripMembership

    profiles: dict[int, Profile] = {}
    if trip.creator_id is not None and trip.creator is not None:
        profiles[trip.creator_id] = trip.creator
    for membership in TripMembership.objects.joined(trip).select_related("profile"):
        profiles[membership.profile_id] = membership.profile
    return list(profiles.values())


def record_trip_activity_shares(activity: TripActivity) -> list[PinShare]:
    """Record detected shares for a freshly added activity to every joined member.

    Args:
        activity: The just-created activity.

    Returns:
        The newly created shares (may be empty).
    """
    pin, location = _activity_place(activity)
    if location is None:
        return []
    sharer = activity.added_by or activity.trip.creator
    if sharer is None:
        return []
    shares = []
    for member in _joined_member_profiles(activity.trip):
        share = _record_detected_trip_share(sharer, member, pin, location)
        if share is not None:
            shares.append(share)
    return shares


def record_trip_shares_for_member(trip: Trip, profile: Profile) -> list[PinShare]:
    """Record detected shares of every place already on ``trip`` to a member who just joined.

    Args:
        trip: The trip being joined.
        profile: The newly joined member.

    Returns:
        The newly created shares (may be empty).
    """
    shares = []
    for activity in trip.activities.select_related("pin__location", "location", "added_by", "trip__creator"):
        pin, location = _activity_place(activity)
        if location is None:
            continue
        sharer = activity.added_by or trip.creator
        if sharer is None or sharer.pk == profile.pk:
            continue
        share = _record_detected_trip_share(sharer, profile, pin, location)
        if share is not None:
            shares.append(share)
    return shares
