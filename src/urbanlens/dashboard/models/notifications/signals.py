"""Live push of newly created notifications to the recipient's open browser tabs.

When a ``NotificationLog`` row is inserted, every browser session the recipient
has open (subscribed via ``UserNotificationConsumer`` at ``ws/notifications/``)
receives the notification over the channel layer immediately, so the bell label
and a toast appear without a page refresh.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.notifications.model import NotificationLog

logger = logging.getLogger(__name__)

#: Longest message body forwarded in the live push payload. The full text stays
#: in the database and is shown in the bell dropdown; the toast only needs a preview.
PUSH_MESSAGE_LIMIT = 300


def notification_group_name(profile_id: int) -> str:
    """Build the channel-layer group name for one profile's live notifications.

    Every open browser session of that profile joins this group, so a single
    ``group_send`` reaches all of the user's tabs at once.

    Args:
        profile_id: Primary key of the recipient's Profile.

    Returns:
        The channel-layer group name.
    """
    return f"profile_notifications_{profile_id}"


def as_push_payload(notification: NotificationLog) -> dict[str, Any]:
    """Serialize a notification into the JSON payload pushed to browsers.

    Args:
        notification: The freshly created notification.

    Returns:
        A JSON-serializable dict with the fields the frontend toast and
        browser Notification need.
    """
    message = notification.message or ""
    if len(message) > PUSH_MESSAGE_LIMIT:
        message = message[:PUSH_MESSAGE_LIMIT].rstrip() + "…"
    return {
        "id": notification.pk,
        "title": notification.title,
        "message": message,
        "url": notification.url,
        "notification_type": notification.notification_type,
        "importance": notification.importance,
    }


@receiver(post_save, sender=NotificationLog, dispatch_uid="notification_live_push")
def push_notification_to_browser(sender: type[NotificationLog], instance: NotificationLog, created: bool, **kwargs: Any) -> None:
    """Broadcast a newly created notification to the recipient's browser sessions.

    The broadcast runs after the transaction commits, so the browser's
    follow-up unread-count fetch is guaranteed to see the new row. A
    channel-layer failure (e.g. Valkey down) is logged and swallowed - live
    delivery is best-effort and must never break notification creation.

    Args:
        sender: The ``NotificationLog`` model class.
        instance: The notification that was saved.
        created: True when the save was an insert.
        **kwargs: Remaining signal arguments (unused).
    """
    if created and instance.profile_id:
        group = notification_group_name(instance.profile_id)
        payload = as_push_payload(instance)

        def _send() -> None:
            layer = get_channel_layer()
            if layer is not None:
                try:
                    async_to_sync(layer.group_send)(group, {"type": "notification.new", "notification": payload})
                except Exception:
                    logger.warning("Live push of notification %s to %s failed; it will appear on the next refresh", payload["id"], group, exc_info=True)

        transaction.on_commit(_send)


@receiver(post_save, sender=NotificationLog, dispatch_uid="notification_native_push")
def enqueue_native_push(sender: type[NotificationLog], instance: NotificationLog, created: bool, **kwargs: Any) -> None:
    """Enqueue delivery of a new notification to the recipient's native devices.

    The WebSocket broadcast above only reaches open browser tabs; a native app
    in the background needs a real push (UnifiedPush/ntfy - see
    ``services.push``). Runs after commit so the Celery worker is guaranteed
    to see the row, and the task itself exits immediately for profiles with no
    registered devices.

    Args:
        sender: The ``NotificationLog`` model class.
        instance: The notification that was saved.
        created: True when the save was an insert.
        **kwargs: Remaining signal arguments (unused).
    """
    if created and instance.profile_id:
        notification_id = instance.pk

        def _enqueue() -> None:
            from urbanlens.dashboard.services.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import dispatch_native_push

            safely_enqueue_task(dispatch_native_push, notification_id)

        transaction.on_commit(_enqueue)
