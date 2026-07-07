"""ProfileEmail - additional (secondary) email addresses a user can be found by."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from django.db.models import CASCADE, BooleanField, CharField, DateTimeField, EmailField, ForeignKey, Q, UniqueConstraint, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.email_normalization import normalize_email


class ProfileEmail(abstract.DashboardModel):
    """An extra email address a user has added to their profile.

    Lets other users find this profile via the invite-friend feature using an
    address other than the account's primary login email. Only ``is_verified``
    rows count for matching (invite-friend lookup, duplicate checks, and
    username-or-email login) - an unverified row is inert so a user can't add
    someone else's address to hijack invites or logins meant for them.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="secondary_emails",
    )
    email = EmailField()
    normalized_email = CharField(max_length=254, db_index=True)
    is_verified = BooleanField(default=False)
    verification_token = UUIDField(default=uuid.uuid4, editable=False)
    verified_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["created"]
        constraints = [
            # Only one profile may hold a *verified* claim on a given normalized
            # address at a time - prevents a second account from verifying an
            # address someone else already verified.
            UniqueConstraint(
                fields=["normalized_email"],
                condition=Q(is_verified=True),
                name="uniq_verified_normalized_email",
            ),
        ]

    def save(self, *args, **kwargs) -> None:
        self.normalized_email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def mark_verified(self) -> None:
        """Record verification without triggering a full model save."""
        from django.utils import timezone

        ProfileEmail.objects.filter(pk=self.pk).update(is_verified=True, verified_at=timezone.now())
        self.is_verified = True
        self.verified_at = timezone.now()

    def __str__(self) -> str:
        return f"ProfileEmail({self.profile_id} → {self.email})"
