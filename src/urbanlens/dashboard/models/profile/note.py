"""ProfileNote - private notes a viewer keeps about another user's profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, TextField

from urbanlens.dashboard.models import abstract

if __import__("typing").TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class ProfileNote(abstract.FrontendDashboardModel):
    """A private note one user keeps about another user's profile.

    The note is visible only to the *author*; the *subject* profile owner
    cannot see it.  A viewer may keep multiple notes per subject.
    """

    content = TextField(blank=True, default="")

    author = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="authored_profile_notes",
    )
    subject = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_profile_notes",
    )

    if TYPE_CHECKING:
        author_id: int
        subject_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"ProfileNote({self.author_id} → {self.subject_id})"
