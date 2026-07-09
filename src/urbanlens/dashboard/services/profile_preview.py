"""Simulate viewing your own profile as another type of user.

The preview works by creating a throwaway "ghost" ``User`` (and the real
relationship rows - friendship, shared pin, mutual friend, or shared trip -
that the selected audience implies) inside a database transaction, rendering
the page through the normal view stack as that ghost, and then rolling the
transaction back so nothing persists.

Because the ghost goes through the exact same controllers, permission checks,
and templates as a real visitor, the preview never needs to be updated when
profile-rendering code changes: whatever a real user with that relationship
would see, the ghost sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from django.contrib.auth.models import User

from urbanlens.dashboard.models.profile.meta import VisibilityChoice

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

SESSION_KEY = "profile_preview"
"""Session key holding the active preview state (mode, label, path, owner id)."""

_MODE_LABEL_OVERRIDES = {
    VisibilityChoice.ANYONE.value: "Any logged-in user",
    VisibilityChoice.FRIENDS.value: "A friend",
    VisibilityChoice.COMMON_PIN.value: "Someone with a pin in common",
    VisibilityChoice.COMMON_FRIEND.value: "Someone with a friend in common",
    VisibilityChoice.COMMON_TRIP.value: "Someone with a trip in common",
    VisibilityChoice.NO_ONE.value: "Anyone else (no connection)",
}


def preview_modes() -> list[tuple[str, str]]:
    """Return the selectable preview audiences as ``(mode, label)`` pairs.

    The list is derived from :class:`VisibilityChoice` so it always mirrors
    the options offered by the privacy controls on the settings page - if a
    new visibility level is added there, it automatically becomes previewable
    here (unknown levels fall back to a no-relationship ghost).

    Returns:
        List of ``(mode value, human-readable label)`` tuples, in the same
        order as the settings-page choices.
    """
    return [(value, _MODE_LABEL_OVERRIDES.get(value, label)) for value, label in VisibilityChoice.choices]


def mode_label(mode: str) -> str:
    """Return the display label for a preview mode value.

    Args:
        mode: A :class:`VisibilityChoice` value.

    Returns:
        The human-readable label, or the raw value for unknown modes.
    """
    return dict(preview_modes()).get(mode, mode)


def create_ghost_viewer(owner: Profile, mode: str) -> User:
    """Create a throwaway user standing in the selected relationship to *owner*.

    Must be called inside a transaction that the caller rolls back - every row
    created here (the ghost user, its auto-created profile, and any
    relationship rows) is meant to exist only for the duration of one request.

    When the owner has no data to share (no pins, friends, or trips), the
    minimum synthetic shared objects are created so the requested relationship
    can still be simulated; these are likewise rolled back.

    Args:
        owner: The profile being previewed (the logged-in user's own profile).
        mode: A :class:`VisibilityChoice` value selecting the relationship.
            Unknown values produce a ghost with no relationship at all.

    Returns:
        The ghost ``User``, whose ``Profile`` was auto-created by the
        ``post_save`` signal.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    ghost_user = User.objects.create_user(username=f"preview_{uuid.uuid4().hex[:16]}")
    ghost = Profile.objects.get(user=ghost_user)

    if mode == VisibilityChoice.FRIENDS:
        _make_friends(ghost, owner)
    elif mode == VisibilityChoice.COMMON_PIN:
        _share_a_pin(ghost, owner)
    elif mode == VisibilityChoice.COMMON_FRIEND:
        _share_a_friend(ghost, owner)
    elif mode == VisibilityChoice.COMMON_TRIP:
        _share_a_trip(ghost, owner)

    return ghost_user


def _make_friends(ghost: Profile, owner: Profile) -> None:
    """Create an accepted friendship between *ghost* and *owner*.

    Args:
        ghost: The throwaway viewer profile.
        owner: The profile being previewed.
    """
    from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
    from urbanlens.dashboard.models.friendship.model import Friendship

    Friendship.objects.create(
        from_profile=ghost,
        to_profile=owner,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
    )


def _share_a_pin(ghost: Profile, owner: Profile) -> None:
    """Give *ghost* a pin at a location the owner has also pinned.

    Uses one of the owner's existing pinned locations when available;
    otherwise fabricates a shared location (and an owner pin) so the
    relationship can still be simulated.

    Args:
        ghost: The throwaway viewer profile.
        owner: The profile being previewed.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    owner_pin = Pin.objects.filter(profile=owner).select_related("location").first()
    if owner_pin is not None:
        location = owner_pin.location
    else:
        from urbanlens.dashboard.models.location.model import Location

        location, _ = Location.objects.get_nearby_or_create(latitude=0.0, longitude=0.0)
        Pin.objects.create(profile=owner, location=location)
    Pin.objects.create(profile=ghost, location=location)


def _share_a_friend(ghost: Profile, owner: Profile) -> None:
    """Give *ghost* a mutual friend with *owner* (without befriending them directly).

    Befriends one of the owner's existing friends when possible; otherwise a
    second ghost is created to act as the mutual friend.

    Args:
        ghost: The throwaway viewer profile.
        owner: The profile being previewed.
    """
    from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.profile.model import Profile

    accepted = Friendship.objects.filter(status=FriendshipStatus.ACCEPTED)
    mutual_id = (
        accepted.filter(from_profile=owner).values_list("to_profile_id", flat=True).first()
        or accepted.filter(to_profile=owner).values_list("from_profile_id", flat=True).first()
    )
    if mutual_id is not None:
        mutual = Profile.objects.get(pk=mutual_id)
    else:
        mutual_user = User.objects.create_user(username=f"preview_{uuid.uuid4().hex[:16]}")
        mutual = Profile.objects.get(user=mutual_user)
        Friendship.objects.create(
            from_profile=mutual,
            to_profile=owner,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )
    Friendship.objects.create(
        from_profile=ghost,
        to_profile=mutual,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
    )


def _share_a_trip(ghost: Profile, owner: Profile) -> None:
    """Put *ghost* on a trip the owner is also a member of.

    Joins one of the owner's existing trips when available; otherwise
    fabricates a trip with both of them as members.

    Args:
        ghost: The throwaway viewer profile.
        owner: The profile being previewed.
    """
    from urbanlens.dashboard.models.trips.model import Trip, TripMembership

    membership = TripMembership.objects.filter(profile=owner).select_related("trip").first()
    if membership is not None:
        trip = membership.trip
    else:
        trip = Trip.objects.create(name="Preview trip", creator=owner)
        TripMembership.objects.create(trip=trip, profile=owner)
    TripMembership.objects.create(trip=trip, profile=ghost)
