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

        Args:
            profile_id: The profile being viewed.
            viewer_id: The profile requesting access.

        Returns:
            True when an unexpired grant exists.
        """
        return cls.objects.filter(profile_id=profile_id, granted_to_id=viewer_id, expires_at__gt=timezone.now()).exists()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_dm_temporary_access"
