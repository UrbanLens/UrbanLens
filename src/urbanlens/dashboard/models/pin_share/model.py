"""Pin sharing models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus


class PinShare(abstract.DashboardModel):
    """A one-to-one share of a single pin from one profile to another."""

    status = models.CharField(max_length=20, choices=PinShareStatus.choices, default=PinShareStatus.PENDING)

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

    if TYPE_CHECKING:
        pin_id: int
        from_profile_id: int
        to_profile_id: int
        notification_id: int | None

    @property
    def is_actionable(self) -> bool:
        return self.status == PinShareStatus.PENDING

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_shares"
        indexes = [
            Index(fields=["to_profile", "status"], name="idxdb_pinshr_to_pfl_stat"),
            Index(fields=["from_profile", "created"], name="idxdb_pinshr_f_pfl_cdt"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["pin", "to_profile"],
                condition=Q(status="pending"),
                name="db_pinshare_one_pending_per_pin_user",
            ),
        ]
