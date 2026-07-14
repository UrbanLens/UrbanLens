"""ProfileLabelAssignment model - private user-label applied to another profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.profile_assignment.queryset import ProfileLabelAssignmentManager


class ProfileLabelAssignment(abstract.DashboardModel):
    """Records that *author* has privately applied a user-type Label to *subject*.

    Only the author can see this assignment; the subject profile owner cannot.
    The label must have kind='user'.
    """

    author = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="profile_label_assignments",
    )
    subject = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_profile_label_assignments",
    )
    label = ForeignKey(
        "dashboard.Label",
        on_delete=CASCADE,
        related_name="profile_assignments",
    )

    if TYPE_CHECKING:
        author_id: int
        subject_id: int
        label_id: int

    objects = ProfileLabelAssignmentManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_label_assignments"
        constraints = [
            UniqueConstraint(
                fields=["author", "subject", "label"],
                name="unique_profile_label_assignment",
            ),
        ]

    def __str__(self) -> str:
        return f"ProfileLabelAssignment({self.author_id} → {self.subject_id}: label={self.label_id})"
