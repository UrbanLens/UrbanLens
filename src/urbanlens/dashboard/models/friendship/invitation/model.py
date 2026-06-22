"""FriendInvitation model - email-based invitation to join UrbanLens."""

from __future__ import annotations

from datetime import timedelta
import uuid

from django.db.models import CASCADE, DateTimeField, EmailField, ForeignKey, UUIDField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.friendship.invitation.queryset import FriendInvitationManager


class FriendInvitation(abstract.Model):
    """Sent when a user invites someone by email who is not yet registered.

    On sign-up the new user's email is matched against open invitations and
    a friend request is automatically sent from the inviter.
    """

    inviter = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="sent_invitations",
    )
    email = EmailField(db_index=True)
    token = UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = DateTimeField()
    accepted_at = DateTimeField(null=True, blank=True)

    objects = FriendInvitationManager()

    class Meta(abstract.Model.Meta):
        pass

    def save(self, *args, **kwargs):
        if not self.pk and not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=14)
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        """Return True if the invitation window has closed."""
        return timezone.now() > self.expires_at

    def is_accepted(self) -> bool:
        """Return True if the invitation has been acted on."""
        return self.accepted_at is not None

    def mark_accepted(self) -> None:
        """Record acceptance time without triggering full-model save."""
        FriendInvitation.objects.filter(pk=self.pk).update(accepted_at=timezone.now())

    def __str__(self) -> str:
        return f"FriendInvitation({self.inviter_id} → {self.email})"
