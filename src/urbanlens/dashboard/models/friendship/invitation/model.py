"""FriendInvitation model - email-based invitation to join UrbanLens."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
import uuid

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, CharField, DateTimeField, EmailField, ForeignKey, TextField, UUIDField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField
from urbanlens.dashboard.models.friendship.invitation.queryset import FriendInvitationManager
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH


class FriendInvitation(abstract.DashboardModel):
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
    # Canonical form of `email` (lowercased, Gmail dots/+suffix stripped - see
    # normalize_email) kept in sync in save() below, mirroring
    # Profile.primary_email_normalized. `email` itself stays as typed since it's
    # still the literal send-target/display value; every cross-account
    # match (signup auto-accept, re-invite dedup) must go through this field
    # instead, or a Gmail variant of an already-invited address silently
    # misses both.
    email_normalized = CharField(max_length=254, blank=True, default="", db_index=True)
    token = UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = DateTimeField()
    accepted_at = DateTimeField(null=True, blank=True)
    # Optional note the inviter attached, shown in the join-invite email.
    # Encrypted: user-authored text about a person who does not yet have an
    # account, only ever read as an attribute. `email` above cannot follow -
    # it is indexed and exact-matched at signup to find open invitations.
    message = EncryptedTextField(
        null=True,
        blank=True,
        fail_soft=True,
        max_length=MAX_FRIEND_REQUEST_MESSAGE_LENGTH,
        validators=[MaxLengthValidator(MAX_FRIEND_REQUEST_MESSAGE_LENGTH)],
    )

    if TYPE_CHECKING:
        inviter_id: int

    objects = FriendInvitationManager()

    class Meta(abstract.DashboardModel.Meta):
        pass

    def save(self, *args, **kwargs):
        from urbanlens.dashboard.services.auth.email_normalization import normalize_email

        if not self.pk and not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=14)
        self.email_normalized = normalize_email(self.email) if self.email else ""
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        """Return True if the invitation window has closed."""
        return timezone.now() > self.expires_at

    def is_accepted(self) -> bool:
        """Return True if the invitation has been acted on."""
        return self.accepted_at is not None

    def mark_accepted(self) -> bool:
        """Claim the invitation with a conditional write, without a full-model save.

        The ``accepted_at__isnull=True`` condition makes this a write-time
        claim: of any number of concurrent redemptions of the same invitation
        (e.g. a double-clicked verification link), exactly one caller sees
        ``True``. Callers must run this *before* the acceptance side effects
        and skip them when it returns ``False``.

        Returns:
            True when this call transitioned the invitation to accepted;
            False when it was already accepted (or no longer exists).
        """
        now = timezone.now()
        claimed = FriendInvitation.objects.filter(pk=self.pk, accepted_at__isnull=True).update(accepted_at=now) == 1
        if claimed:
            self.accepted_at = now
            # Recorded here rather than by a post_save subscription: this
            # transition is a queryset update() precisely so it is an atomic
            # compare-and-set, and update() fires no signal. A rule subscribed
            # to FriendInvitation saves would never see an acceptance.
            from urbanlens.dashboard.services.reputation.scoring import record_event

            record_event(self.inviter_id, "invite_accepted", target=self)
        return claimed

    def __str__(self) -> str:
        return f"FriendInvitation({self.inviter_id} → {self.email})"
