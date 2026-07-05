"""Shared helpers for looking up a profile's accepted friend connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def get_connections(profile: Profile) -> list[Profile]:
    """Return the profiles this profile has an accepted (mutual) friendship with.

    Args:
        profile: Profile whose connections should be listed.

    Returns:
        List of connected Profile instances.
    """
    friendships = Friendship.objects.all().profile(profile.pk).is_friend().select_related("from_profile__user", "to_profile__user")
    return [f.to_profile if f.from_profile_id == profile.pk else f.from_profile for f in friendships]


def are_connections(a: Profile, b: Profile) -> bool:
    """Return whether two profiles have an accepted (mutual) friendship.

    Args:
        a: First profile.
        b: Second profile.

    Returns:
        True if a and b are mutually connected.
    """
    friendship = Friendship.objects.all().between(a, b)
    return bool(friendship and friendship.status == FriendshipStatus.ACCEPTED)
