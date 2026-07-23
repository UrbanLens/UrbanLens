"""DirectMessageLocationMention - coordinates/address detected in a message's text.

One row per distinct place detected in one direct message (see
``services.dm_location_detection``). The mention is what the chat UI renders
under the bubble: an "Add to map" button for the recipient when the place
counted as a share, or - for the recipient only, since it is their private
data - the name of their own existing pin at that place.

The mention row is presentation-level and dies with its message; the
``pin_share`` it may have produced (and that share's ``LocationExposure``)
deliberately survives message deletion - information, once received, cannot
be un-received, so the share chain must keep counting it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index, UniqueConstraint

from urbanlens.dashboard.models import abstract


class LocationMentionKind(abstract.TextChoices):
    """What kind of text produced a mention."""

    COORDINATES = "coordinates", "Coordinates"
    ADDRESS = "address", "Street Address"


class DirectMessageLocationMention(abstract.DashboardModel):
    """One place detected in the text of one direct message."""

    message = models.ForeignKey("dashboard.DirectMessage", on_delete=models.CASCADE, related_name="location_mentions")
    location = models.ForeignKey("dashboard.Location", on_delete=models.CASCADE, related_name="+")
    # The DM_DETECTED share this mention produced. None when it didn't count
    # as a share - the recipient already had the place pinned (the mention
    # then renders their own pin's name instead of an "Add to map" button).
    pin_share = models.ForeignKey(
        "dashboard.PinShare",
        on_delete=models.SET_NULL,
        related_name="dm_mentions",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=20, choices=LocationMentionKind.choices, default=LocationMentionKind.COORDINATES)
    # The exact text that matched, for display ("40.7128, -74.0060").
    matched_text = models.CharField(max_length=255, blank=True, default="")

    if TYPE_CHECKING:
        message_id: int
        location_id: int
        pin_share_id: int | None

    @property
    def can_add_to_map(self) -> bool:
        """Whether the "Add to map" action is currently available for this mention.

        True while the backing share can still be accepted - PENDING
        (DM-detected) or DETECTED (the place reached the recipient earlier
        via map/trip detection, which never auto-materializes a pin).

        Returns:
            True when the recipient may create a pin from this mention.
        """
        from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

        return self.pin_share is not None and self.pin_share.status in (PinShareStatus.PENDING, PinShareStatus.DETECTED)

    def recipient_pin(self):
        """The message recipient's own top-level pin at this place, if any.

        Private data: only ever render the result to the pin's owner (the
        message recipient) - never to the sender.

        Returns:
            The recipient's Pin near this mention's location, or None.
        """
        from urbanlens.dashboard.services.share_provenance import find_profile_pin_near_location

        return find_profile_pin_near_location(self.message.recipient_id, self.location)

    def __str__(self) -> str:
        """Return a human-readable description of this mention.

        Returns:
            String like "Mention message=5 location=9 (coordinates)".
        """
        return f"Mention message={self.message_id} location={self.location_id} ({self.kind})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_dm_location_mentions"
        ordering = ["created"]
        indexes = [
            Index(fields=["message"], name="idxdb_dmlocm_message"),
        ]
        constraints = [
            UniqueConstraint(fields=["message", "location"], name="db_dmlocm_one_per_msg_loc"),
        ]
