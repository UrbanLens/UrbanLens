"""Business logic for direct messages between users.

Mirrors the safety check-in chat pipeline (``services.safety.create_chat_message``
and its consumer/broadcast pattern): validation and persistence live here, both
the WebSocket consumer (``DirectMessageConsumer``) and the no-JS HTTP fallback
call the same ``create_direct_message``, and live delivery over the channel
layer is strictly best-effort - a Valkey hiccup must never lose a message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def direct_message_group_name(profile_id: int) -> str:
    """Build the channel-layer group name for one profile's live direct messages.

    Every open browser session of that profile joins this group (via
    ``DirectMessageConsumer``), so one ``group_send`` reaches all tabs at once.

    Args:
        profile_id: Primary key of the profile.

    Returns:
        The channel-layer group name.
    """
    return f"profile_direct_messages_{profile_id}"


def can_direct_message(sender: Profile, recipient: Profile) -> bool:
    """Return True when ``sender`` is allowed to message ``recipient``.

    Args:
        sender: The profile attempting to send.
        recipient: The profile being messaged.

    Returns:
        True when both profiles have community features enabled and the
        recipient's ``direct_message_visibility`` setting (or the always-allowed
        reply exception) permits the sender.
    """
    if sender.pk == recipient.pk:
        return False
    if not sender.community_enabled or not recipient.community_enabled:
        return False
    return recipient.accepts_direct_messages_from(sender)


def serialize_direct_message(message: DirectMessage) -> dict[str, Any]:
    """Serialize a message into the JSON payload pushed over the WebSocket.

    Args:
        message: The message to serialize.

    Returns:
        A JSON-serializable dict; ``sender_slug``/``recipient_slug`` let the
        frontend route the payload to the right open conversation.
    """
    return {
        "type": "message",
        "id": message.pk,
        "body": message.body,
        "created": message.created.isoformat(),
        "sender_slug": message.sender.slug or "",
        "sender_name": message.sender.username,
        "recipient_slug": message.recipient.slug or "",
    }


def _broadcast_direct_message(message: DirectMessage) -> None:
    """Push a new message to both participants' live sessions after commit.

    Best-effort, mirroring ``services.safety._broadcast_chat_message``: the row
    is already durably saved, so a channel-layer failure is logged, not raised -
    the recipient still sees the message on their next page load or badge poll.

    Args:
        message: The freshly created message.
    """
    payload = serialize_direct_message(message)
    groups = {direct_message_group_name(message.sender_id), direct_message_group_name(message.recipient_id)}

    def _send() -> None:
        layer = get_channel_layer()
        if layer is None:
            return
        for group in groups:
            try:
                async_to_sync(layer.group_send)(group, {"type": "dm.message", "message": payload})
            except Exception:
                logger.warning("Live push of direct message %s to %s failed; it will appear on refresh", message.pk, group, exc_info=True)

    transaction.on_commit(_send)


def _notify_recipient(message: DirectMessage) -> None:
    """Raise an on-site notification for the recipient, honoring their preference.

    Only fires when the recipient had no other unread messages from this sender -
    one notification per "conversation went unread", not one per message, so an
    active back-and-forth doesn't flood the bell dropdown.

    Args:
        message: The freshly created message.
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    try:
        pref = message.recipient.notification_preferences.message
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return

    already_unread = DirectMessage.objects.filter(sender=message.sender, recipient=message.recipient, read_at__isnull=True).exclude(pk=message.pk).exists()
    if already_unread:
        return

    preview = message.body if len(message.body) <= 120 else message.body[:120].rstrip() + "…"
    NotificationLog.objects.create(
        profile=message.recipient,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.MESSAGE,
        title=f"New message from {message.sender.username}",
        message=preview,
        url=reverse("messages.conversation", kwargs={"profile_slug": message.sender.ensure_slug()}),
        source_profile=message.sender,
    )


def create_direct_message(sender: Profile, recipient: Profile, body: str) -> DirectMessage:
    """Validate, persist, broadcast, and notify for one new direct message.

    Args:
        sender: The profile sending the message.
        recipient: The profile receiving it.
        body: Message text.

    Returns:
        The newly created DirectMessage.

    Raises:
        ValueError: If ``body`` is blank or exceeds ``MAX_DIRECT_MESSAGE_LENGTH``.
        PermissionError: If the recipient's privacy settings don't permit the
            sender. Callers surface this as a 403 / socket error message.
    """
    body = body.strip()
    if not body:
        raise ValueError("Message cannot be empty.")
    if len(body) > MAX_DIRECT_MESSAGE_LENGTH:
        raise ValueError(f"Message is too long (max {MAX_DIRECT_MESSAGE_LENGTH:,} characters).")
    if not can_direct_message(sender, recipient):
        raise PermissionError("This user isn't accepting messages from you.")

    message = DirectMessage.objects.create(sender=sender, recipient=recipient, body=body)
    logger.info("Direct message %s: profile %s -> profile %s", message.pk, sender.pk, recipient.pk)
    _notify_recipient(message)
    _broadcast_direct_message(message)
    return message


def conversations_for(profile: Profile) -> list[dict[str, Any]]:
    """Return the profile's conversations, most recently active first.

    Args:
        profile: The profile whose inbox to build.

    Returns:
        A list of dicts with ``partner`` (Profile), ``last_message``
        (DirectMessage), and ``unread_count`` (int).
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    rows = list(DirectMessage.objects.conversation_rows(profile))
    if not rows:
        return []

    partners = ProfileModel.objects.select_related("user").in_bulk([row["partner_id"] for row in rows])
    last_messages = DirectMessage.objects.in_bulk([row["last_message_id"] for row in rows])

    conversations = []
    for row in rows:
        partner = partners.get(row["partner_id"])
        last_message = last_messages.get(row["last_message_id"])
        if partner is None or last_message is None:
            continue
        conversations.append(
            {
                "partner": partner,
                "last_message": last_message,
                "unread_count": row["unread_count"],
            },
        )
    return conversations


def has_used_direct_messages(profile: Profile) -> bool:
    """Return True when the profile has ever sent or received a direct message.

    Gates the navbar messages icon: users who have never touched the feature
    don't get an extra icon competing for attention.

    Args:
        profile: The profile to check.

    Returns:
        True when at least one message involves the profile.
    """
    return DirectMessage.objects.involving(profile).exists()
