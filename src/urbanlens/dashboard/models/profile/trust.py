"""ProfileTrust - private trust rating a viewer assigns to another user's profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import CASCADE, ForeignKey, IntegerField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.profile.queryset import ProfileTrustManager


class ProfileTrust(abstract.DashboardModel):
    """A private 1-5 star trust rating one user keeps about another user's profile.

    Only the *author* can see their own rating; the *subject* profile owner
    cannot.  Each author may hold at most one trust rating per subject.
    """

    rating = IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Trust level from 1 (low) to 5 (high).",
    )

    author = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="authored_trust_ratings",
    )
    subject = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_trust_ratings",
    )

    objects = ProfileTrustManager()

    if TYPE_CHECKING:
        author_id: int
        subject_id: int

    class Meta(abstract.DashboardModel.Meta):
        constraints = [
            UniqueConstraint(
                fields=["author", "subject"],
                name="unique_profile_trust_rating",
            ),
        ]

    def __str__(self) -> str:
        return f"ProfileTrust({self.author_id} → {self.subject_id}: {self.rating}★)"
