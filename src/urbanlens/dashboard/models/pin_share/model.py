"""Pin sharing models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus


class PinShare(abstract.DashboardModel):
    """A one-to-one share of a single pin from one profile to another.

    Shares form a tree: when the shared pin itself arrived via an earlier
    share (the sharer accepted someone else's share and is now passing the
    place along), ``parent_share`` points at that earlier share. Walking the
    ``reshares`` relation transitively yields every downstream share of the
    same place, which powers the Memories → Sharing chain counts.
    """

    status = models.CharField(max_length=20, choices=PinShareStatus.choices, default=PinShareStatus.PENDING)

    pin = models.ForeignKey("dashboard.Pin", on_delete=models.CASCADE, related_name="shares")
    from_profile = models.ForeignKey("dashboard.Profile", on_delete=models.CASCADE, related_name="sent_pin_shares")
    to_profile = models.ForeignKey("dashboard.Profile", on_delete=models.CASCADE, related_name="received_pin_shares")
    # The share through which the sharer originally received this place, when
    # the pin they are sharing was created by accepting another share.
    parent_share = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="reshares",
        null=True,
        blank=True,
    )
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
        parent_share_id: int | None
        notification_id: int | None

    @property
    def is_actionable(self) -> bool:
        return self.status == PinShareStatus.PENDING

    @classmethod
    def chain_share_count(cls, root_share_ids: list[int]) -> int:
        """Total number of shares in the trees rooted at the given shares.

        Counts the roots themselves plus every transitive reshare below them
        (breadth-first over ``parent_share``), so "A shared with B, B shared
        with C and D, D shared with E and F" counts 5 for A's share.

        Args:
            root_share_ids: Primary keys of the shares to start from.

        Returns:
            The total share count down the chain, including the roots.
        """
        seen: set[int] = set(root_share_ids)
        frontier = list(seen)
        while frontier:
            children = list(cls.objects.filter(parent_share_id__in=frontier).exclude(pk__in=seen).values_list("pk", flat=True))
            seen.update(children)
            frontier = children
        return len(seen)

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
