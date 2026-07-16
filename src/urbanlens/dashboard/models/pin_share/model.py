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
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class PinShare(abstract.DashboardModel):
    """A one-to-one share of a single place from one profile to another.

    Shares form a tree: when the shared pin itself arrived via an earlier
    share (the sharer accepted someone else's share and is now passing the
    place along), ``parent_share`` points at that earlier share. Walking the
    ``reshares`` relation transitively yields every downstream share of the
    same place, which powers the Memories → Sharing chain counts.

    A share always tracks *both* halves of the place model: ``pin`` (the
    sharer's mutable personal record, when they have one) and ``location``
    (the immutable shared place, snapshotted at share time). Tracking both
    keeps chains honest when pins are later moved, deleted, or re-created -
    see ``models.pin_share.exposure`` for the full rationale. ``pin`` is None
    for location-only shares, e.g. raw coordinates or a street address
    detected in a direct message when the sender never pinned the place.
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

    # The direct message whose text revealed this place, when origin is
    # DM_DETECTED (coordinates or a street address typed into a chat).
    detected_via_message = models.ForeignKey(
        "dashboard.DirectMessage",
        on_delete=models.SET_NULL,
        related_name="detected_pin_shares",
        null=True,
        blank=True,
    )

    # The sharer's pin, when they have one. None for location-only shares
    # (e.g. coordinates detected in a DM the sender never pinned).
    pin = models.ForeignKey("dashboard.Pin", on_delete=models.CASCADE, related_name="shares", null=True, blank=True)
    # The immutable Location of the shared place, snapshotted at share time.
    # Always set for new shares (from pin.location when a pin is present) so
    # the share survives the pin later being moved or deleted. SET_NULL keeps
    # the row (and its chain counts) even if the Location itself ever goes.
    location = models.ForeignKey(
        "dashboard.Location",
        on_delete=models.SET_NULL,
        related_name="pin_shares",
        null=True,
        blank=True,
    )
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
        pin_id: int | None
        location_id: int | None
        from_profile_id: int
        to_profile_id: int
        parent_share_id: int | None
        bundled_with_id: int | None
        notification_id: int | None
        detected_via_map_id: int | None
        detected_via_message_id: int | None
        markup_map_id: int | None

    @property
    def is_actionable(self) -> bool:
        return self.status == PinShareStatus.PENDING

    @property
    def shared_location_id(self) -> int | None:
        """PK of the shared place's Location - the snapshot, else the pin's current one.

        Prefers the ``location`` snapshot taken at share time: the sharer may
        have moved their pin somewhere else since, and this share is about
        where the pin was when it was shared.

        Returns:
            The Location pk, or None for legacy rows whose pin is gone.
        """
        if self.location_id is not None:
            return self.location_id
        return self.pin.location_id if self.pin is not None else None

    @property
    def shared_location(self) -> Location | None:
        """The shared place's Location (see ``shared_location_id``)."""
        if self.location_id is not None:
            return self.location
        return self.pin.location if self.pin is not None else None

    @property
    def place_label(self) -> str:
        """Human-readable label of the shared place, safe for pin-less shares.

        Returns:
            The pin's display label when a pin exists, otherwise the shared
            location's display name / address / coordinates.
        """
        if self.pin is not None:
            return self.pin.display_label
        location = self.shared_location
        if location is None:
            return "a location"
        if location.display_name and location.display_name != "Unnamed Location":
            return location.display_name
        return location.address or f"{location.latitude}, {location.longitude}"

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
        location_id = self.shared_location_id
        if location_id is None:
            return None
        from urbanlens.dashboard.models.pin.model import Pin

        query = Pin.objects.filter(profile=self.to_profile, parent_pin__isnull=True, location_id=location_id)
        if self.pin_id is not None:
            query = query.exclude(pk=self.pin_id)
        return query.first()

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
            Index(fields=["to_profile", "location"], name="idxdb_pinshr_to_pfl_loc"),
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
