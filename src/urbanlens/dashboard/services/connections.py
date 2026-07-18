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

    Used when someone is added to a shared space (a trip, a group chat) that
    already has other members they aren't friends with yet. A pair is
    eligible only when neither side has opted out - this is a *mutual*
    introduction (both sides get suggested to each other), unlike an
    in-chat ``@friend`` recommendation where only the recommended person's
    ``allow_friend_recommendations`` is checked.

    Args:
        new_member: The profile that was just added.
        others: Other members of the same shared space.

    Returns:
        The subset of ``others`` eligible for a suggestion with ``new_member``.
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    if not new_member.allow_friend_recommendations:
        return []
    return [
        other
        for other in others
        if other.pk != new_member.pk
        and other.allow_friend_recommendations
        and not are_connections(new_member, other)
        and not ProfileModel.are_blocked(new_member, other)
    ]


def suggest_mutual_connection(a: Profile, b: Profile) -> None:
    """Softly introduce two unconnected profiles who now share a trip/group.

    Deliberately does *not* grant profile-view access the way an in-chat
    ``@friend`` recommendation does (``services.direct_message_shares.recommend_friend_in_message``)
    - that mechanism exists for a friend to deliberately vouch for someone
    and open a window onto their own profile. Automatically doing the same
    here, just because two strangers now share a trip/group, would let
    merely being added to a shared space silently bypass a subject's own
    ``profile_visibility`` (e.g. ``NO_ONE``) as a side effect of
    ``allow_friend_recommendations`` - a setting about recommending someone
    to other people, not about unlocking their own locked-down profile.
    This only sends each profile a notification naming the other - if they
    want to connect, they send a friend request themselves.

    Callers should gate this on ``recommendable_strangers`` first - this
    function does not re-check eligibility.

    Args:
        a: One of the two profiles to introduce.
        b: The other profile to introduce.
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity

    # No dedicated NotificationPreference field for this type (same as
    # AI_EXTRACTION) - it's a one-off soft suggestion, not a recurring
    # notification category worth its own settings-page toggle. The profile
    # link is safe to include even for a NO_ONE-visibility subject: without
    # a temporary-access grant (deliberately not created here), that page
    # re-checks and correctly re-masks on its own - this notification isn't
    # a second, separate way around that gate. The message text still names
    # the subject through resolve_visible_identity rather than their raw
    # username, for the same reason - a masked person's real username must
    # not leak into a notification the viewer wasn't otherwise shown it in.
    for viewer, subject in ((a, b), (b, a)):
        display_name = resolve_visible_identity(viewer, subject)["display_name"]
        NotificationLog.objects.create(
            profile=viewer,
            source_profile=subject,
            status=Status.UNREAD,
            importance=Importance.LOW,
            notification_type=NotificationType.FRIEND_SUGGESTION,
            title="You might know each other",
            message=f"You and {display_name} are both in the same trip or group chat - want to connect?",
            url=reverse("profile.view_user", kwargs={"profile_slug": subject.slug}) if subject.slug else "",
        )
