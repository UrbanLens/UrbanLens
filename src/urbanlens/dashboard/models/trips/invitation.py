"""TripInvitation - an invitation to a trip sent to an email address."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
import uuid

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, Q, UniqueConstraint, UUIDField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: How long an unanswered invitation stays open.
TRIP_INVITATION_LIFETIME = timedelta(days=30)


class TripInvitationResponse:
    """The invitee's answer to one of the invitation's two independent questions."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CHOICES = [(PENDING, "Pending"), (ACCEPTED, "Accepted"), (DECLINED, "Declined")]


class TripInvitationQuerySet(abstract.FrontendDashboardQuerySet["TripInvitation"]):
    """Queries over trip invitations."""

    def open(self) -> TripInvitationQuerySet:
        """Invitations whose trip question is unanswered and unexpired."""
        return self.filter(trip_response=TripInvitationResponse.PENDING, expires_at__gt=timezone.now())

    def withdrawable(self) -> TripInvitationQuerySet:
        """Unexpired invitations with either question still unanswered."""
        return self.filter(Q(trip_response=TripInvitationResponse.PENDING) | Q(friend_response=TripInvitationResponse.PENDING), expires_at__gt=timezone.now())

    def for_address(self, email_hash: str) -> TripInvitationQuerySet:
        """Invitations sent to one normalized address."""
        return self.filter(email_hash=email_hash)

    def unbound(self) -> TripInvitationQuerySet:
        """Invitations not yet tied to an account."""
        return self.filter(invitee__isnull=True)


class TripInvitationManager(abstract.FrontendDashboardManager.from_queryset(TripInvitationQuerySet)):
    """Manager for trip invitations."""


class TripInvitation(abstract.FrontendDashboardModel):
    """A trip invitation addressed to an email, which may or may not belong to an account.

    The inviter only ever sees the address they typed, so whether it is registered stays hidden. The invitee
    answers two questions independently: whether to join the trip, and whether to become friends with the
    inviter. Registering never answers either.
    """

    trip = ForeignKey("dashboard.Trip", on_delete=CASCADE, related_name="email_invitations")
    inviter = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="sent_trip_invitations")
    # The address as typed, shown only to the inviter.
    email = EncryptedTextField(fail_soft=True, blank=True, default="")
    email_hash = CharField(max_length=64, db_index=True)
    # Never shown to the inviter.
    invitee = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="received_trip_invitations")
    # The invitee's capability; never shown to the inviter.
    token = UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = DateTimeField()
    trip_response = CharField(max_length=10, choices=TripInvitationResponse.CHOICES, default=TripInvitationResponse.PENDING)
    friend_response = CharField(max_length=10, choices=TripInvitationResponse.CHOICES, default=TripInvitationResponse.PENDING)

    objects = TripInvitationManager()

    if TYPE_CHECKING:
        trip_id: int
        inviter_id: int
        invitee_id: int | None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_trip_invitations"
        constraints = [
            UniqueConstraint(fields=["trip", "inviter", "email_hash"], name="trip_invitation_one_per_inviter_address"),
        ]
        indexes = []

    def save(self, *args, **kwargs) -> None:
        if not self.pk and not self.expires_at:
            self.expires_at = timezone.now() + TRIP_INVITATION_LIFETIME
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        """Whether the invitation window has closed."""
        return timezone.now() > self.expires_at

    def is_open(self) -> bool:
        """Whether the trip question can still be answered."""
        return self.trip_response == TripInvitationResponse.PENDING and not self.is_expired()

    def addressed_to(self, profile: Profile) -> bool:
        """Whether ``profile`` may answer: the bound invitee, or the account that verified the address of an unbound one.

        Holding the link is not enough; it can be forwarded.
        """
        from urbanlens.dashboard.services.auth.email_normalization import has_verified_address

        if self.inviter_id == profile.pk:
            return False
        if self.invitee_id is not None:
            return self.invitee_id == profile.pk
        return has_verified_address(profile.user, self.email)

    def __str__(self) -> str:
        return f"TripInvitation({self.inviter_id} → trip {self.trip_id})"
