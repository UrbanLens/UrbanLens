"""Shared helpers for looking up a profile's accepted friend connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def recommendable_strangers(new_member: Profile, others: Sequence[Profile]) -> list[Profile]:
    """Return which of ``others`` are eligible for a mutual "you might want to connect" suggestion with ``new_member``.

    Args:
        new_member: The profile that was just added.
        others: Other members of the same shared space.

    Returns:
        The subset of ``others`` eligible for a suggestion with ``new_member``."""
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    if not new_member.allow_friend_recommendations:
        return []
    return [other for other in others if other.pk != new_member.pk and other.allow_friend_recommendations and not are_connections(new_member, other) and not ProfileModel.are_blocked(new_member, other)]


def suggest_mutual_connection(a: Profile, b: Profile) -> None:
    """Softly introduce two unconnected profiles who now share a trip/group.
    Automatically doing the same here, just because two strangers now share a trip/group, would let merely being added to a shared space silently bypass a subject's own ``profile_visibility`` (e.g.

    Args:
        a: One of the two profiles to introduce.
        b: The other profile to introduce."""
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    # No dedicated NotificationPreference field for this type (same as AI_EXTRACTION) - it's a
    # one-off soft suggestion, not a recurring notification category worth its own settings-page
    # toggle.
    # The profile link is safe to include even for a NO_ONE-visibility subject: without a
    for viewer, subject in ((a, b), (b, a)):
        display_name = resolve_visible_identity(viewer, subject)["display_name"]
        NotificationLog.objects.notify(
            profile=viewer,
            source_profile=subject,
            status=Status.UNREAD,
            importance=Importance.LOW,
            notification_type=NotificationType.FRIEND_SUGGESTION,
            title="You might know each other",
            message=f"You and {display_name} are both in the same trip or group chat - want to connect?",
            url=reverse("profile.view_user", kwargs={"profile_slug": subject.slug}) if subject.slug else "",
        )
