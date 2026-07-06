"""NotificationLog and NotificationPreference models."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.notifications.meta import (
    DeliveryPreference,
    Importance,
    NotificationType,
    Status,
)
from urbanlens.dashboard.models.notifications.queryset import NotificationManager

logger = logging.getLogger(__name__)


class NotificationLog(abstract.Model):
    """Records a notification sent to a specific user profile."""

    status = models.CharField(max_length=17, choices=Status.choices, default=Status.UNREAD)
    importance = models.CharField(max_length=17, choices=Importance.choices, default=Importance.LOWEST)
    notification_type = models.CharField(max_length=20, choices=NotificationType.choices, default=NotificationType.INFO)
    title = models.CharField(max_length=255, blank=True)
    message = models.CharField(max_length=50000, blank=True)
    url = models.CharField(max_length=500, blank=True)

    profile = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )
    source_profile = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.SET_NULL,
        related_name="triggered_notifications",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        profile_id: int | None
        source_profile_id: int | None
        pin_share_id: int | None

    objects = NotificationManager()

    @property
    def is_unread(self) -> bool:
        """True when this notification has not been read yet."""
        return self.status == Status.UNREAD

    @property
    def is_friend_request_pending(self) -> bool:
        """True when this is a friend_request notification still awaiting a response.

        Deliberately independent of read status: opening the notification dropdown
        marks notifications read, but the Accept/Decline buttons must stay visible
        until the recipient actually accepts or declines the request.
        """
        if self.notification_type != NotificationType.FRIEND_REQUEST or not self.source_profile_id or not self.profile_id:
            return False

        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
        from urbanlens.dashboard.models.friendship.model import Friendship

        friendship = Friendship.objects.between(self.source_profile_id, self.profile_id)
        return friendship is not None and friendship.status == FriendshipStatus.REQUESTED

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_notifications"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["profile", "status"], name="idxdb_notif_pfile_stat"),
            Index(fields=["status"], name="idxdb_notif_status"),
            Index(fields=["importance"], name="idxdb_notif_import"),
            Index(fields=["notification_type"], name="idxdb_notif_type"),
        ]


class NotificationPreference(abstract.Model):
    """Per-user delivery preferences for each notification type."""

    profile = models.OneToOneField(
        "dashboard.Profile",
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    trip_updated = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    friend_request = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    message = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    comment_reply = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    comment_liked = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    friend_accepted = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    added_to_trip = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    wiki_updated = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    pin_shared = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)
    visit_suggested = models.CharField(max_length=10, choices=DeliveryPreference.choices, default=DeliveryPreference.SITE)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_notification_preferences"
