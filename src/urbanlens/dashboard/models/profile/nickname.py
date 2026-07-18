"""ProfileNickname - private nickname a viewer assigns to another user's profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.profile.queryset import ProfileNicknameManager


class ProfileNickname(abstract.DashboardModel):
    """A private nickname one user assigns to another user's profile.

    Only the *author* can see the nickname they assigned; the *subject*
    profile owner cannot.  Each author may hold at most one nickname per
    subject.
    """

    nickname = CharField(
        max_length=100,
        help_text="Private nickname the author uses to refer to the subject.",
    )

    author = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="authored_nicknames",
    )
    subject = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_nicknames",
    )

    objects = ProfileNicknameManager()

    if TYPE_CHECKING:
        author_id: int
        subject_id: int

    class Meta(abstract.DashboardModel.Meta):
        constraints = [
            UniqueConstraint(
                fields=["author", "subject"],
                name="unique_profile_nickname",
            ),
        ]

    def __str__(self) -> str:
        return f"ProfileNickname({self.author_id} → {self.subject_id}: {self.nickname!r})"
