"""Pin sharing models."""

from __future__ import annotations

from django.db import models
from django.db.models import Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract


class PinShareStatus(abstract.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    ALREADY_PINNED = "already_pinned", "Already pinned"


class PinShare(abstract.Model):
    """A one-to-one share of a single pin from one profile to another."""

    pin = models.ForeignKey("dashboard.Pin", on_delete=models.CASCADE, related_name="shares")
    from_profile = models.ForeignKey("dashboard.Profile", on_delete=models.CASCADE, related_name="sent_pin_shares")
    to_profile = models.ForeignKey("dashboard.Profile", on_delete=models.CASCADE, related_name="received_pin_shares")
    notification = models.OneToOneField(
        "dashboard.NotificationLog",
        on_delete=models.SET_NULL,
        related_name="pin_share",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=PinShareStatus.choices, default=PinShareStatus.PENDING)

    @property
    def is_actionable(self) -> bool:
        return self.status == PinShareStatus.PENDING

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_shares"
        indexes = [
            Index(fields=["to_profile", "status"]),
            Index(fields=["from_profile", "created"]),
        ]
        constraints = [
            UniqueConstraint(
                fields=["pin", "to_profile"],
                condition=Q(status="pending"),
                name="dashboard_pin_share_one_pending_per_pin_user",
            ),
        ]
