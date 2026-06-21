"""ProfileNote - a private, per-viewer note attached to another user's profile."""

from __future__ import annotations

from django.db.models import CASCADE, ForeignKey, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract

if __import__("typing").TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class ProfileNote(abstract.Model):
    """A private note one user keeps about another user's profile.

    The note is visible only to the *author*; the *subject* profile owner
    cannot see it.  There is at most one note per (author, subject) pair.
    """

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
    content = TextField(blank=True, default="")

    class Meta(abstract.Model.Meta):
        constraints = [
            UniqueConstraint(fields=["author", "subject"], name="unique_profile_note"),
        ]

    def __str__(self) -> str:
        return f"ProfileNote({self.author_id} → {self.subject_id})"
