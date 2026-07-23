"""Standalone "share this map with a friend" action."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, SET_NULL, ForeignKey, Index, OneToOneField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH


class MarkupMapShare(abstract.DashboardModel):
    """A one-to-one share of a standalone MarkupMap from one profile to another.

    Unlike PinShare there is no accept/reject workflow - this model exists
    purely to grant the recipient a permission-checked view of someone else's
    map and to carry the notification, mirroring PinShareDetailView's
    to_profile-scoped access pattern without any of PinShare's materialization
    machinery. The recipient's only action is to optionally clone the map into
    their own account (see services.map_sharing.clone_markup_map).
    """

    markup_map = ForeignKey("dashboard.MarkupMap", on_delete=CASCADE, related_name="shares")
    from_profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="sent_map_shares")
    to_profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="received_map_shares")
    # Optional note from the sharer.
    message = TextField(
        null=True,
        blank=True,
        max_length=MAX_PIN_SHARE_MESSAGE_LENGTH,
        validators=[MaxLengthValidator(MAX_PIN_SHARE_MESSAGE_LENGTH)],
    )
    notification = OneToOneField(
        "dashboard.NotificationLog",
        on_delete=SET_NULL,
        related_name="map_share",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        markup_map_id: int
        from_profile_id: int
        to_profile_id: int
        notification_id: int | None

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_markup_map_shares"
        indexes = [
            Index(fields=["to_profile"], name="idxdb_mms_to_profile"),
            Index(fields=["markup_map"], name="idxdb_mms_markup_map"),
        ]
