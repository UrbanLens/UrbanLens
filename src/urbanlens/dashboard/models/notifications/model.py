"""NotificationLog and NotificationPreference models."""
from __future__ import annotations

import logging

from django.db import models
from django.db.models import Index

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.notifications.meta import (
    DeliveryPreference,
    Importance,
    NotificationType,
    Status,
)
from urbanlens.dashboard.models.notifications.queryset import Manager

logger = logging.getLogger(__name__)


class NotificationLog(abstract.Model):
    """Records a notification sent to a specific user profile."""

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
    status = models.CharField(max_length=17, choices=Status.choices, default=Status.UNREAD)
    importance = models.CharField(max_length=17, choices=Importance.choices, default=Importance.LOWEST)
    notification_type = models.CharField(max_length=20, choices=NotificationType.choices, default=NotificationType.INFO)
    title = models.CharField(max_length=255, blank=True)
    message = models.CharField(max_length=50000, blank=True)
    url = models.CharField(max_length=500, blank=True)

    objects = Manager()

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_notifications"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["profile", "status"]),
            Index(fields=["status"]),
            Index(fields=["importance"]),
            Index(fields=["notification_type"]),
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

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_notification_preferences"
