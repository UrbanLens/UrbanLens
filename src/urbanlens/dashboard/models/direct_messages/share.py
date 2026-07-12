"""DirectMessageShare - an `@pin` / `@trip` / `@friend` share embedded in a message."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, OneToOneField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.meta import DirectMessageShareKind


class DirectMessageShare(abstract.DashboardModel):
    """The `@pin`/`@trip`/`@friend` action attached to one direct message.

    Exactly one of `pin_share`, (`trip` + `trip_membership`), or
    `recommended_profile` is populated, matching `kind`. `revoke()` is called
    when the message is deleted, and is a no-op once the underlying action has
    actually been acted on (accepted/rejected/responded) - deleting the
    message never undoes something the recipient already did.
    """

    kind = CharField(max_length=20, choices=DirectMessageShareKind.choices)

    message = OneToOneField(
        "dashboard.DirectMessage",
        on_delete=CASCADE,
        related_name="share",
    )

    # kind=PIN
    pin_share = ForeignKey(
        "dashboard.PinShare",
        on_delete=SET_NULL,
        related_name="direct_message_share",
        null=True,
        blank=True,
    )

    # kind=TRIP
    trip = ForeignKey(
        "dashboard.Trip",
        on_delete=SET_NULL,
        related_name="direct_message_shares",
        null=True,
        blank=True,
    )
    trip_membership = ForeignKey(
        "dashboard.TripMembership",
        on_delete=SET_NULL,
        related_name="direct_message_share",
        null=True,
        blank=True,
    )

    # kind=FRIEND
    recommended_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )

    revoked_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        message_id: int
        pin_share_id: int | None
        trip_id: int | None
        trip_membership_id: int | None
        recommended_profile_id: int | None

    @property
    def is_actionable(self) -> bool:
        """True while this share can still be revoked (nothing was done with it yet)."""
        if self.revoked_at is not None:
            return False
        if self.kind == DirectMessageShareKind.PIN:
            from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

            return self.pin_share is not None and self.pin_share.status == PinShareStatus.PENDING
        if self.kind == DirectMessageShareKind.TRIP:
            return self.trip_membership is not None and self.trip_membership.rsvp is None
        if self.kind == DirectMessageShareKind.FRIEND:
            return not self._friend_request_exists()
        return False

    def _friend_request_exists(self) -> bool:
        """True if a Friendship row already links the recommended profile and the recipient."""
        from django.db.models import Q

        from urbanlens.dashboard.models.friendship.model import Friendship

        if self.recommended_profile_id is None:
            return False
        recipient = self.message.recipient
        return Friendship.objects.filter(
            Q(from_profile=self.recommended_profile, to_profile=recipient) | Q(from_profile=recipient, to_profile=self.recommended_profile),
        ).exists()

    def revoke(self) -> None:
        """Undo this share's effect, but only if the recipient hasn't acted on it yet.

        Called when the owning message is deleted. Already-accepted pin shares,
        already-responded trip invites, and friend recommendations that
        resulted in a request are left completely alone - there is nothing to
        revoke once the recipient has acted.
        """
        if self.revoked_at is not None:
            return

        if self.kind == DirectMessageShareKind.PIN and self.pin_share is not None:
            from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

            if self.pin_share.status == PinShareStatus.PENDING:
                self.pin_share.status = PinShareStatus.REJECTED
                self.pin_share.save(update_fields=["status"])
        elif self.kind == DirectMessageShareKind.TRIP and self.trip_membership is not None:
            if self.trip_membership.rsvp is None:
                self.trip_membership.delete()
                self.trip_membership = None
        elif self.kind == DirectMessageShareKind.FRIEND and self._friend_request_exists():
            return

        self.revoked_at = timezone.now()
        update_fields = ["revoked_at", "trip_membership"] if self.kind == DirectMessageShareKind.TRIP else ["revoked_at"]
        self.save(update_fields=update_fields)

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_direct_message_shares"
