"""Who may see a trip, and who may act on it.

The single source of truth for trip authorization, shared by the internal
HTMX controllers and the external REST API so a permission-level change can
never apply to one surface and not the other.

Historically these lived as private helpers on ``controllers.trip``
(``_trip_or_403``, ``_is_organizer``, ``_viewer_has_joined``,
``_can_perform``). They moved here when the external API needed the same
rules; the controller now calls these directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.trip_errors import TripNotFoundError, TripPermissionError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: What a viewer is told when a trip either does not exist or is not theirs.
#: One string for both cases on purpose - see :func:`get_trip_for_viewer`.
TRIP_NOT_FOUND_MESSAGE = "No such trip."


def get_trip_for_viewer(trip_slug: str, viewer: Profile) -> Trip:
    """Return the trip identified by *trip_slug*, if *viewer* may see it at all.

    Membership (or being the creator) is the viewing gate; contributing is a
    separate, stricter question handled by :func:`can_perform`. An invited
    member who has not yet joined can still read the trip.

    A missing trip and a trip the viewer has no access to raise the *same*
    error with the *same* message. The previous implementation
    (``controllers.trip._trip_or_403``) rendered the same "not found" page for
    both but answered 404 for one and 403 for the other, which let anyone
    enumerate valid private trip slugs by reading the status code alone -
    exactly what the identical page was meant to prevent. Both are 404 now.

    Args:
        trip_slug: The trip's URL slug.
        viewer: The profile asking to see it.

    Returns:
        The trip.

    Raises:
        TripNotFoundError: No such trip, or the viewer is neither its creator
            nor one of its members.
    """
    trip = Trip.objects.filter(slug=trip_slug).select_related("creator__user").first()
    if trip is None:
        raise TripNotFoundError(TRIP_NOT_FOUND_MESSAGE)
    if trip.creator_id == viewer.id or TripMembership.objects.for_trip_and_profile(trip, viewer).exists():
        return trip
    raise TripNotFoundError(TRIP_NOT_FOUND_MESSAGE)


def is_organizer(profile: Profile, trip: Trip) -> bool:
    """Return True when *profile* is the trip's creator or a designated organizer.

    Args:
        profile: The profile to test.
        trip: The trip.

    Returns:
        True for the creator or any member flagged ``is_organizer``.
    """
    if trip.creator_id == profile.id:
        return True
    return TripMembership.objects.for_trip_and_profile(trip, profile).filter(is_organizer=True).exists()


def has_joined(profile: Profile, trip: Trip) -> bool:
    """Return True when *profile* may contribute at all - creator, or a joined member.

    An invited member can view the trip (see :func:`get_trip_for_viewer`) but
    cannot contribute (add/edit activities, comment, vote, add members) until
    they accept the invitation.

    Args:
        profile: The profile to test.
        trip: The trip.

    Returns:
        True for the creator or a member whose status is ``joined``.
    """
    if trip.creator_id == profile.id:
        return True
    return TripMembership.objects.for_trip_and_profile(trip, profile).filter(status=TripMembership.STATUS_JOINED).exists()


def can_perform(profile: Profile, trip: Trip, level: str) -> bool:
    """Return True when *profile* is allowed to act at the given permission level.

    Requires the profile to have joined (see :func:`has_joined`) - an
    invited-but-not-yet-joined member can never act, regardless of level.
    Otherwise the creator is always allowed, ``everyone`` allows any joined
    member, ``organizers`` requires organizer/creator status, and ``none``
    allows only the creator.

    Args:
        profile: The profile attempting the action.
        trip: The trip being acted on.
        level: One of ``Trip.PERM_NONE``/``PERM_ORGANIZERS``/``PERM_EVERYONE``,
            normally read from the matching ``trip.allow_*`` field.

    Returns:
        True when the action is permitted.
    """
    if trip.creator_id == profile.id:
        return True
    if not has_joined(profile, trip):
        return False
    if level == Trip.PERM_EVERYONE:
        return True
    if level == Trip.PERM_ORGANIZERS:
        return TripMembership.objects.for_trip_and_profile(trip, profile).filter(is_organizer=True).exists()
    return False


def require_perform(profile: Profile, trip: Trip, level: str, message: str) -> None:
    """Raise unless *profile* may act at *level* on *trip*.

    Args:
        profile: The profile attempting the action.
        trip: The trip being acted on.
        level: The required permission level (see :func:`can_perform`).
        message: The refusal message shown to the user.

    Raises:
        TripPermissionError: The profile may not act at this level.
    """
    if not can_perform(profile, trip, level):
        raise TripPermissionError(message)


def require_joined(profile: Profile, trip: Trip, message: str) -> None:
    """Raise unless *profile* has joined *trip*.

    The level-independent counterpart to :func:`require_perform`, for actions
    (RSVP, viewing suggestions, editing trip metadata) gated only on having
    accepted the invitation.

    Args:
        profile: The profile attempting the action.
        trip: The trip being acted on.
        message: The refusal message shown to the user.

    Raises:
        TripPermissionError: The profile has not joined the trip.
    """
    if not has_joined(profile, trip):
        raise TripPermissionError(message)
