"""The one place a trip's roster grows, so the member cap is checked and spent in one locked step.

Every path that adds a member - invite by username, a friend picked at creation, an accepted email
invitation, a calendar import, a data-export restore - counts the roster and then writes to it. Unserialised,
two of them at once both read the same count and both fit under a cap only one of them had room for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.trips.trip_errors import TripQuotaError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

TRIP_FULL = "This trip is full ({max_members} members maximum)."


def lock_trip_roster(trip: Trip) -> int:
    """Serialise roster changes on *trip* for the rest of the current transaction.

    Args:
        trip: The trip whose roster is about to change.

    Returns:
        Seats left under ``SiteSettings.max_trip_members``; zero or less when full.
    """
    Trip.objects.select_for_update().filter(pk=trip.pk).first()
    return SiteSettings.get_current().max_trip_members - trip.profiles.count()


def reserve_trip_seat(trip: Trip, profile: Profile, *, status: str = TripMembership.STATUS_INVITED, **defaults: Any) -> tuple[TripMembership, bool]:
    """Put *profile* on *trip*'s roster if there is room; a profile already on it keeps its row.

    Args:
        trip: The trip.
        profile: Who is being added.
        status: The new membership's status.
        **defaults: Other fields for a new membership, e.g. ``rsvp``.

    Returns:
        The ``(membership, created)`` pair.

    Raises:
        TripQuotaError: The trip is full.
    """
    with transaction.atomic():
        remaining = lock_trip_roster(trip)
        existing = TripMembership.objects.filter(trip=trip, profile=profile).first()
        if existing is not None:
            return existing, False
        if remaining <= 0:
            raise TripQuotaError(TRIP_FULL.format(max_members=SiteSettings.get_current().max_trip_members))
        return TripMembership.objects.create(trip=trip, profile=profile, status=status, **defaults), True
