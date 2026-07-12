"""Shared pin-share creation logic, reused by the `@pin` chat-sharing feature.

The standalone share dialog (``controllers.pin_sharing.PinShareCreateView``)
has its own richer flow (custom names, bundled child pins, photo selection)
and is left as-is; this module holds just the create-and-notify core so a
second, simpler caller (chat) doesn't have to duplicate the friends-only rule
or the notification wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import reverse

from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.services.connections import are_connections

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def recipient_existing_pin(profile: Profile, source: Pin) -> Pin | None:
    """Return the recipient's own top-level pin at the same Location as `source`, if any.

    Args:
        profile: The prospective recipient.
        source: The pin being considered for sharing.

    Returns:
        The recipient's matching pin, or None.
    """
    if not source.location_id:
        return None
    return Pin.objects.filter(profile=profile, parent_pin__isnull=True, location_id=source.location_id).first()


def create_pin_share(sender: Profile, recipient: Profile, pin: Pin, *, message: str | None = None, shared_name: str | None = None) -> PinShare:
    """Create a PinShare (and its notification), enforcing the friends-only sharing rule.

    Args:
        sender: The profile sharing the pin (must own it).
        recipient: The profile the pin is being shared with.
        pin: The pin being shared.
        message: Optional note to attach.
        shared_name: Optional override name for the shared pin.

    Returns:
        The newly created PinShare.

    Raises:
        PermissionError: If `sender` and `recipient` aren't connected friends.
    """
    if recipient.pk == sender.pk or not are_connections(sender, recipient):
        raise PermissionError("Pins can only be shared with connected friends.")

    already_pinned = recipient_existing_pin(recipient, pin) is not None
    share = PinShare.objects.create(
        pin=pin,
        from_profile=sender,
        to_profile=recipient,
        parent_share_id=pin.source_share_id,
        status=PinShareStatus.ALREADY_PINNED if already_pinned else PinShareStatus.PENDING,
        message=message,
        shared_name=shared_name,
    )
    base_message = f"{sender.username} shared {pin.display_label} with you."
    if already_pinned:
        base_message += " You already have this location pinned."
    notification = NotificationLog.objects.create(
        profile=recipient,
        source_profile=sender,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.PIN_SHARED,
        title="Pin shared with you",
        message=base_message,
        url=reverse("pin.share.detail", kwargs={"share_id": share.pk}),
    )
    share.notification = notification
    share.save(update_fields=["notification", "updated"])
    return share
