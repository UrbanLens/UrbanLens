"""Registered native-client push destinations (UnifiedPush endpoints, FCM tokens).
The browser gets live notifications over the Channels WebSocket (``models.notifications.signals``); a native app in the background does not hold a socket open, so it registers a push destination here instead and the server delivers through it (``services.notifications.push``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, DateTimeField, ForeignKey, Index, PositiveIntegerField, TextChoices, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.push_device.queryset import PushDeviceManager


class PushTransport(TextChoices):
    """How a registered device receives pushes."""

    UNIFIEDPUSH = "unifiedpush", "UnifiedPush endpoint (e.g. ntfy)"
    FCM = "fcm", "Firebase Cloud Messaging token"


class PushDevice(abstract.FrontendDashboardModel):
    """One native client's push destination, owned by a profile.
    ``address`` is the UnifiedPush endpoint URL (or FCM registration token) - treated as an opaque, secret-ish value: anyone holding a UnifiedPush URL can send to that device, so it is never exposed through any read API.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="push_devices")
    transport = CharField(max_length=20, choices=PushTransport.choices, default=PushTransport.UNIFIEDPUSH)
    address = CharField(max_length=500)
    #: User-facing device label, e.g. "Pixel 9" - shown in a future settings UI.
    name = CharField(max_length=100, blank=True, default="")
    last_success_at = DateTimeField(null=True, blank=True)
    failure_count = PositiveIntegerField(default=0)
    revoked_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        id: int
        profile_id: int

    objects = PushDeviceManager()

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_push_devices"
        ordering = ["-created"]
        indexes = [
            Index(fields=["profile", "revoked_at"], name="idxdb_pushdev_prof_revoked"),
        ]
        constraints = [
            UniqueConstraint(fields=["profile", "address"], name="db_push_device_unique_address"),
        ]

    @property
    def dispatch_enabled(self) -> bool:
        """Whether ``services.notifications.push`` will actually send to this device. Only UnifiedPush is dispatched.

        Returns:
            True when this device's transport is dispatched today.
        """
        return self.transport == PushTransport.UNIFIEDPUSH

    def __str__(self) -> str:
        status = "revoked" if self.revoked_at else "active"
        return f"PushDevice(profile={self.profile_id}, transport={self.transport}, {status})"
