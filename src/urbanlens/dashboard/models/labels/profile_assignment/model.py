"""ProfileBadgeAssignment model - private user-badge applied to another profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.badges.profile_assignment.queryset import ProfileBadgeAssignmentManager


class ProfileBadgeAssignment(abstract.DashboardModel):
    """Records that *author* has privately applied a user-type Badge to *subject*.

    Only the author can see this assignment; the subject profile owner cannot.
    The badge must have kind='user'.
    """

    author = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="profile_badge_assignments",
    )
    subject = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_profile_badge_assignments",
    )
    badge = ForeignKey(
        "dashboard.Badge",
        on_delete=CASCADE,
        related_name="profile_assignments",
    )

    if TYPE_CHECKING:
        author_id: int
        subject_id: int
        badge_id: int

    objects = ProfileBadgeAssignmentManager()

    class Meta(abstract.DashboardModel.Meta):
        constraints = [
            UniqueConstraint(
                fields=["author", "subject", "badge"],
                name="unique_profile_badge_assignment",
            ),
        ]

    def __str__(self) -> str:
        return f"ProfileBadgeAssignment({self.author_id} → {self.subject_id}: badge={self.badge_id})"
