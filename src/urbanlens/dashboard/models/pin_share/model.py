"""Pin sharing models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Index, ManyToManyField, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class PinShare(abstract.DashboardModel):
    """A one-to-one share of a single pin from one profile to another.

    Shares form a tree: when the shared pin itself arrived via an earlier
    share (the sharer accepted someone else's share and is now passing the
    place along), ``parent_share`` points at that earlier share. Walking the
    ``reshares`` relation transitively yields every downstream share of the
    same place, which powers the Memories → Sharing chain counts.
    """

    status = models.CharField(max_length=20, choices=PinShareStatus.choices, default=PinShareStatus.PENDING)
    # How this share came to exist - an explicit share-a-pin action, or
    # auto-detected because a MarkupMap sent to `to_profile` revealed this
    # pin (see services.map_pin_share_detection.detect_shared_pins).
    origin = models.CharField(max_length=20, choices=PinShareOrigin.choices, default=PinShareOrigin.EXPLICIT)
    # The MarkupMap whose detection produced this share, when origin is
    # MAP_DETECTED. Distinct from `markup_map` below.
    detected_via_map = models.ForeignKey(
        "dashboard.MarkupMap",
        on_delete=models.SET_NULL,
        related_name="detected_pin_shares",
        null=True,
        blank=True,
    )
    # An optional map the sharer chose to attach when explicitly sharing this
    # pin (mirrors DirectMessage.markup_map). Distinct from `detected_via_map`
    # above, which records the map that triggered auto-detection rather than
    # one deliberately attached to this share.
    markup_map = models.ForeignKey(
        "dashboard.MarkupMap",
        on_delete=models.SET_NULL,
        related_name="pin_share_attachments",
        null=True,
        blank=True,
    )

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
    # The root share of a "pin + its sub pins" bundle. When a sharer opts to
    # include a pin's child pins, each child pin gets its own PinShare row
    # (it counts as a share of that pin) pointing here; the recipient accepts
    # or rejects the whole bundle through the root share, and accepting
    # recreates the parent/child hierarchy on their side.
    bundled_with = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        related_name="bundled_shares",
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
    # Optional note from the sharer explaining why they're sharing this place.
    message = models.TextField(
        null=True,
        blank=True,
        max_length=MAX_PIN_SHARE_MESSAGE_LENGTH,
        validators=[MaxLengthValidator(MAX_PIN_SHARE_MESSAGE_LENGTH)],
    )
    # Name to present for the shared pin, chosen by the sharer at share time -
    # one of the pin's existing aliases, or a brand-new name (which also gets
    # added to the sharer's own PinAlias list). Blank means "use the pin's
    # current effective name" at both share and accept time.
    shared_name = models.CharField(max_length=255, null=True, blank=True)
    # Photos the sharer opted to include - a subset of pin.images. Kept as a
    # reference to the sharer's own Image rows; accepting the share copies
    # these onto the recipient's new pin (see _create_pin_from_share).
    images = ManyToManyField("dashboard.Image", blank=True, related_name="pin_shares")

    if TYPE_CHECKING:
        pin_id: int
        from_profile_id: int
        to_profile_id: int
        parent_share_id: int | None
        bundled_with_id: int | None
        notification_id: int | None
        detected_via_map_id: int | None
        markup_map_id: int | None

    @property
    def is_actionable(self) -> bool:
        return self.status == PinShareStatus.PENDING

    @property
    def resulting_pin(self) -> Pin | None:
        """The recipient-side Pin this share produced, once accepted.

        Covers both accept paths: a brand-new Pin (``source_share`` points
        back here) and the "recipient already had this place pinned" dedup
        case (no `source_share` link, so it's found by location instead).

        Returns:
            The recipient's Pin, or None if this share isn't accepted (yet).
        """
        if self.status != PinShareStatus.ACCEPTED:
            return None
        created = self.pins_created.first()
        if created is not None:
            return created
        from urbanlens.dashboard.models.pin.model import Pin

        return Pin.objects.filter(profile=self.to_profile, parent_pin__isnull=True, location_id=self.pin.location_id).exclude(pk=self.pin_id).first()

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
            # Race-safety backstop for the application-level dedup check in
            # services.map_sharing._record_detected_share - at most one
            # MAP_DETECTED share per (pin, recipient) pair.
            UniqueConstraint(
                fields=["pin", "to_profile"],
                condition=Q(origin="map_detected"),
                name="db_pinshare_one_detected_per_pin_user",
            ),
        ]
