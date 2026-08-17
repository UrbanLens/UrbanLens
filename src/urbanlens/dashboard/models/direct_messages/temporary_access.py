"""Short-lived profile-view grants, e.g. from an `@friend` recommendation in chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, DateTimeField, ForeignKey
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class DirectMessageTemporaryAccess(abstract.DashboardModel):
    """Grants `granted_to` a time-boxed ability to view `profile`'s profile page.

    Used when one chat participant recommends a friend to the other: the
    recipient of the recommendation counts as a "friend" of the recommended
    profile, for profile-view purposes only, until `expires_at` - long enough
    to decide whether to actually send a friend request.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="temporary_access_grants",
    )
    granted_to = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="temporary_access_received",
    )
    expires_at = DateTimeField()

    if TYPE_CHECKING:
        profile_id: int
        granted_to_id: int

    @property
    def is_active(self) -> bool:
        """True while this grant has not yet expired."""
        return timezone.now() < self.expires_at

    @classmethod
    def grants_access(cls, profile_id: int, viewer_id: int) -> bool:
        """Return True if an active grant lets `viewer_id` view `profile_id`'s profile.

        A BLOCKED relationship in either direction vetoes the grant even
        while it is unexpired - a block placed after the recommendation was
        made must kill the access immediately, and recommendations to a
        blocked party are refused at creation time as well (see
        ``services.messaging.direct_message_shares.recommend_friend_in_message``).

        Args:
            profile_id: The profile being viewed.
            viewer_id: The profile requesting access.

        Returns:
            True when an unexpired grant exists and neither profile has
            blocked the other.
        """
        if not cls.objects.filter(profile_id=profile_id, granted_to_id=viewer_id, expires_at__gt=timezone.now()).exists():
            return False

        from django.db.models import Q

        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        return not Friendship.objects.filter(
            Q(from_profile_id=profile_id, to_profile_id=viewer_id) | Q(from_profile_id=viewer_id, to_profile_id=profile_id),
            status=FriendshipStatus.BLOCKED,
        ).exists()

    @classmethod
    def granted_profile_pks(cls, profile_ids: set[int], viewer_id: int) -> set[int]:
        """Batch equivalent of :meth:`grants_access` for many profiles at once.

        Same rule, same veto: an unexpired grant to ``viewer_id``, cancelled by a
        BLOCKED relationship in either direction. Answering one profile at a time costs
        two queries each, which is what made rendering a list of people scale.

        Args:
            profile_ids: The profiles being viewed.
            viewer_id: The profile requesting access.

        Returns:
            The subset of ``profile_ids`` that ``viewer_id`` may view via a grant.
        """
        if not profile_ids:
            return set()

        from django.db.models import Q

        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        granted = set(
            cls.objects.filter(profile_id__in=profile_ids, granted_to_id=viewer_id, expires_at__gt=timezone.now()).values_list("profile_id", flat=True),
        )
        if not granted:
            return set()

        blocked = set(
            Friendship.objects.filter(from_profile_id__in=granted, to_profile_id=viewer_id, status=FriendshipStatus.BLOCKED).values_list("from_profile_id", flat=True),
        ) | set(
            Friendship.objects.filter(to_profile_id__in=granted, from_profile_id=viewer_id, status=FriendshipStatus.BLOCKED).values_list("to_profile_id", flat=True),
        )
        return granted - blocked

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_dm_temporary_access"
