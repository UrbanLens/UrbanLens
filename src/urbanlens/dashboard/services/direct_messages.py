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
from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.global_search.parser import ParsedQuery

logger = logging.getLogger(__name__)

#: Common emoji offered by the quick "add a reaction" picker on each message.
REACTION_PICKER_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉"]

#: Maximum number of messages loaded per page of a conversation thread. Both
#: the initial thread view (most recent page) and each "load older history"
#: fetch pull this many at a time, so a years-long conversation never has to
#: query and render its entire history just to open the thread.
THREAD_PAGE_SIZE = 50

#: Characters a reaction "emoji" must never contain. Reactions are broadcast
#: verbatim to the other participant and rendered into their DOM, so a value
#: carrying HTML/JS metacharacters (or plain letters that spell a tag/handler)
#: is rejected outright - genuine emoji, including keycap digits like ``#`` and
#: ``*``, use none of these. Defense in depth: the clients already render the
#: emoji as text/attributes, never as markup.
_REACTION_EMOJI_FORBIDDEN = set("<>&\"'`=/\\{}") | set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def is_safe_reaction_emoji(emoji: str) -> bool:
    """Return True when `emoji` is a plausible, render-safe reaction glyph.

    Args:
        emoji: The candidate reaction string (already length-capped by the caller).

    Returns:
        True when it is non-empty and contains no HTML/JS-significant characters
        or ASCII letters; False otherwise.
    """
    return bool(emoji) and not any(character in _REACTION_EMOJI_FORBIDDEN for character in emoji)


def _online_cache_key(profile_id: int) -> str:
    return f"dm_online_{profile_id}"


def mark_profile_online(profile_id: int) -> None:
    """Record one more live DM socket connection for `profile_id`.

    Uses a connection *counter* (not a simple flag with a TTL) so a profile
    with several open tabs/devices only goes "offline" once every connection
    has closed. Best-effort: a missed decrement (e.g. server crash) leaves the
    profile showing online until the next natural connect/disconnect cycle
    corrects it - an acceptable tradeoff for a presence indicator, matching
    this module's other best-effort broadcasts.

    Args:
        profile_id: PK of the profile that just connected.
    """
    key = _online_cache_key(profile_id)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=None)


def mark_profile_offline(profile_id: int) -> None:
    """Record one fewer live DM socket connection for `profile_id`.

    Args:
        profile_id: PK of the profile that just disconnected.
    """
    key = _online_cache_key(profile_id)
    try:
        remaining = cache.decr(key)
    except ValueError:
        return
    if remaining <= 0:
        cache.delete(key)


def is_profile_online(profile: Profile) -> bool:
    """Return True while `profile` has at least one live DM socket connection.

    Args:
        profile: The profile to check.

    Returns:
        True if online.
    """
    count = cache.get(_online_cache_key(profile.pk))
    return bool(count) and count > 0


def broadcast_typing_indicator(sender_id: int, recipient_slug: str) -> None:
    """Relay a "typing" event to `recipient_slug`, honoring the sender's privacy setting.

    Args:
        sender_id: PK of the profile that is typing.
        recipient_slug: Slug of the profile who should see the indicator.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    try:
        sender = Profile.objects.get(pk=sender_id)
        recipient = Profile.objects.get(slug=recipient_slug)
    except Profile.DoesNotExist:
        return
    if not Profile.visibility_permits(sender.typing_indicator_visibility, sender, recipient):
        return

    layer = get_channel_layer()
    if layer is None:
        return
    payload = {"type": "typing", "sender_slug": sender.slug or ""}
    try:
        async_to_sync(layer.group_send)(direct_message_group_name(recipient.pk), {"type": "dm.message", "message": payload})
    except Exception:
        logger.warning("Live push of typing indicator from profile %s to %s failed", sender_id, recipient.pk, exc_info=True)


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
    reply_to = None
    if message.reply_to_id and message.reply_to is not None:
        quoted = message.reply_to
        if quoted.is_encrypted:
            # The server can't produce a plaintext preview - ship the quoted
            # ciphertext so the recipient's client decrypts its own preview.
            quoted_preview = "🔒 Message"
        elif quoted.body:
            quoted_preview = quoted.body[:80]
        else:
            quoted_preview = "📷 Photo" if quoted.images.exists() else ("🗺️ Map" if quoted.markup_map_id else "Message")
        reply_to = {
            "id": quoted.pk,
            "sender_name": quoted.sender.username,
            "preview": quoted_preview,
            "ciphertext": quoted.ciphertext,
            "nonce": quoted.nonce,
            "key_version": quoted.key_version,
        }

    return {
        "type": "message",
        "id": message.pk,
        "body": message.body,
        "ciphertext": message.ciphertext,
        "nonce": message.nonce,
        "key_version": message.key_version,
        "created": message.created.isoformat(),
        "sender_slug": message.sender.slug or "",
        "sender_name": message.sender.username,
        "recipient_slug": message.recipient.slug or "",
        "images": [{"id": image.pk, "url": image.image.url} for image in message.images.all()],
        "images_revealed": message.images_revealed,
        "markup_map_uuid": str(message.markup_map.uuid) if message.markup_map is not None else None,
        # Only a cheap presence flag - the actual share (pin/trip/friend
        # fields, current status) needs the full `_message_share_card.html`
        # render, so the client re-fetches the thread partial instead of
        # trying to reconstruct that markup from JSON (same reason
        # `markup_map_uuid` doesn't ship the whole map).
        "has_share": getattr(message, "share", None) is not None,
        # Same server-render contract as `has_share`: detected coordinate/
        # address mentions render via `_message_location_mentions.html`, so
        # the client re-fetches the thread partial instead of building the
        # footer from JSON.
        "has_location_mentions": message.location_mentions.exists(),
        "reply_to": reply_to,
    }


def _broadcast_direct_message(message: DirectMessage) -> None:
    """Push a new message to both participants' live sessions after commit.

    Best-effort, mirroring ``services.safety._broadcast_chat_message``: the row
    is already durably saved, so a channel-layer failure is logged, not raised -
    the recipient still sees the message on their next page load or label poll.

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

    from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    try:
        pref = message.recipient.notification_preferences.message
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return

    if DirectMessageMute.objects.filter(viewer=message.recipient, sender=message.sender).exists():
        return

    already_unread = DirectMessage.objects.filter(sender=message.sender, recipient=message.recipient, read_at__isnull=True).exclude(pk=message.pk).exists()
    if already_unread:
        return

    if message.is_encrypted:
        preview = "🔒 Encrypted message"
    elif message.body:
        preview = message.body if len(message.body) <= 120 else message.body[:120].rstrip() + "…"
    elif message.markup_map_id:
        preview = "🗺️ Shared a map"
    elif message.images.exists():
        preview = "📷 Shared a photo"
    else:
        preview = "New message"
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


#: How long a client's "I have this conversation open" marker stays valid
#: without being refreshed - comfortably longer than the client's resend
#: interval (see pages/messages/index.html), so a brief network hiccup
#: doesn't spuriously mark the thread closed.
_OPEN_THREAD_TTL_SECONDS = 90

#: How long after an unread message arrives before the "new message" email
#: fires, giving a logged-in user a chance to read it organically first.
EMAIL_DELAY_SECONDS = 120

#: How long the "already emailed about this unread streak" marker lasts -
#: effectively forever relative to the delay itself, since it's cleared
#: explicitly the moment the recipient views the conversation.
_EMAIL_DEBOUNCE_TTL_SECONDS = 60 * 60 * 24


def _open_thread_cache_key(profile_id: int) -> str:
    return f"dm_open_thread_{profile_id}"


def mark_thread_open(profile_id: int, partner_id: int) -> None:
    """Record that `profile_id`'s client currently has the thread with `partner_id` open.

    Args:
        profile_id: PK of the profile viewing the thread.
        partner_id: PK of the conversation partner.
    """
    cache.set(_open_thread_cache_key(profile_id), partner_id, timeout=_OPEN_THREAD_TTL_SECONDS)


def is_thread_open(profile_id: int, partner_id: int) -> bool:
    """Return True if `profile_id` currently has the thread with `partner_id` open.

    Args:
        profile_id: PK of the profile who would be viewing the thread.
        partner_id: PK of the conversation partner.

    Returns:
        True if that thread is the one currently open for that profile.
    """
    return cache.get(_open_thread_cache_key(profile_id)) == partner_id


def _email_debounce_key(sender_id: int, recipient_id: int) -> str:
    return f"dm_email_sent_{sender_id}_{recipient_id}"


def is_email_debounced(sender_id: int, recipient_id: int) -> bool:
    """Return True if an email was already sent for this sender/recipient's current unread streak.

    Args:
        sender_id: PK of the message sender.
        recipient_id: PK of the recipient.

    Returns:
        True if the debounce marker is set.
    """
    return bool(cache.get(_email_debounce_key(sender_id, recipient_id)))


def clear_email_debounce(sender_id: int, recipient_id: int) -> None:
    """Clear the "already emailed" marker so the next unread streak can email again.

    Called whenever `recipient_id` views their conversation with `sender_id`.

    Args:
        sender_id: PK of the message sender.
        recipient_id: PK of the recipient who just viewed the thread.
    """
    cache.delete(_email_debounce_key(sender_id, recipient_id))


def _schedule_message_email(message: DirectMessage) -> None:
    """Queue the delayed "new message" email, honoring the recipient's preference.

    Args:
        message: The freshly created, still-unread message.
    """
    from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

    try:
        pref = message.recipient.notification_preferences.message
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref not in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        return

    if DirectMessageMute.objects.filter(viewer=message.recipient, sender=message.sender).exists():
        return

    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import send_direct_message_email_if_unread

    safely_enqueue_task(send_direct_message_email_if_unread, message.pk, countdown=EMAIL_DELAY_SECONDS)


def send_message_email_now(message: DirectMessage) -> None:
    """Send the "new message" email for `message`, marking the unread streak as emailed.

    Called by the Celery task once the send delay has elapsed - `message`
    must still be unread and not already debounced by an earlier email in the
    same unread streak (both checked by the caller).

    Args:
        message: The message to email about.
    """
    import smtplib

    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    recipient_email = message.recipient.user.email
    if not recipient_email:
        return

    cache.set(_email_debounce_key(message.sender_id, message.recipient_id), 1, timeout=_EMAIL_DEBOUNCE_TTL_SECONDS)

    if message.is_encrypted:
        # End-to-end encrypted - the server has no plaintext to preview.
        preview = ""
    else:
        preview = message.body if len(message.body) <= 200 else message.body[:200].rstrip() + "…"
    conversation_path = reverse("messages.conversation", kwargs={"profile_slug": message.sender.ensure_slug()})
    conversation_url = f"{settings.SITE_URL.rstrip('/')}{conversation_path}"
    context = {"sender": message.sender, "recipient": message.recipient, "preview": preview, "conversation_url": conversation_url}
    subject = f"New message from {message.sender.username}"
    if preview:
        text_body = f"{message.sender.username} sent you a message on UrbanLens:\n\n{preview}\n\nReply: {conversation_url}"
    else:
        text_body = f"{message.sender.username} sent you a message on UrbanLens.\n\nReply: {conversation_url}"
    html_body = render_to_string("dashboard/email/new_direct_message.html", context)

    try:
        from django.core.mail import EmailMultiAlternatives

        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[recipient_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send new-message email to %s", recipient_email)


def create_direct_message(
    sender: Profile,
    recipient: Profile,
    body: str,
    *,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    image_ids: list[int] | None = None,
    markup_map_uuid: str | None = None,
    reply_to_id: int | None = None,
    defer_broadcast: bool = False,
) -> DirectMessage:
    """Validate, persist, broadcast, and notify for one new direct message.

    Args:
        sender: The profile sending the message.
        recipient: The profile receiving it.
        body: Plaintext message text. Must be blank when ``ciphertext`` is
            given; may be blank if at least one attachment is given.
        ciphertext: End-to-end encrypted body (base64), produced client-side
            under the conversation key. Mutually exclusive with ``body``.
        nonce: Base64 nonce for ``ciphertext`` (required with it).
        key_version: ``ConversationKey.version`` that encrypted this message.
        image_ids: PKs of the sender's own not-yet-attached ``Image`` rows
            (uploaded separately beforehand) to attach to this message.
        markup_map_uuid: UUID of a ``MarkupMap`` owned by the sender to attach.
        reply_to_id: PK of an earlier message in this conversation to quote.
        defer_broadcast: When True, skip the live WebSocket push - the caller
            is attaching a ``DirectMessageShare`` to this message right after
            and must call ``broadcast_direct_message`` once that's done, so
            `serialize_direct_message`'s ``has_share`` flag is correct on the
            wire instead of racing a second, duplicate "message" event.

    Returns:
        The newly created DirectMessage.

    Raises:
        ValueError: If both ``body`` and ``ciphertext`` are given, both (and
            every attachment) are absent, or either exceeds its length limit.
        PermissionError: If the recipient's privacy settings don't permit the
            sender. Callers surface this as a 403 / socket error message.
    """
    from urbanlens.dashboard.services.e2ee import MAX_CIPHERTEXT_LENGTH, MAX_NONCE_LENGTH, valid_blob

    body = body.strip()
    if len(body) > MAX_DIRECT_MESSAGE_LENGTH:
        raise ValueError(f"Message is too long (max {MAX_DIRECT_MESSAGE_LENGTH:,} characters).")
    if ciphertext:
        if body:
            raise ValueError("A message is either plaintext or encrypted, never both.")
        if not valid_blob(ciphertext, MAX_CIPHERTEXT_LENGTH) or not valid_blob(nonce, MAX_NONCE_LENGTH) or key_version < 1:
            raise ValueError("Malformed encrypted message.")
    elif nonce or key_version:
        raise ValueError("Malformed encrypted message.")
    if not body and not ciphertext and not image_ids and not markup_map_uuid:
        raise ValueError("Message cannot be empty.")
    if not can_direct_message(sender, recipient):
        raise PermissionError("This user isn't accepting messages from you.")

    from urbanlens.dashboard.models.markup.model import MarkupMap

    markup_map = None
    if markup_map_uuid:
        markup_map = MarkupMap.objects.filter(uuid=markup_map_uuid, profile=sender).first()

    reply_to = None
    if reply_to_id:
        reply_to = DirectMessage.objects.between(sender, recipient).filter(pk=reply_to_id).first()

    message = DirectMessage.objects.create(
        sender=sender,
        recipient=recipient,
        body=body,
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=key_version,
        markup_map=markup_map,
        reply_to=reply_to,
        sender_delete_after=sender.direct_message_delete_after,
    )

    if image_ids:
        from urbanlens.dashboard.models.images.model import Image

        attached = Image.objects.filter(pk__in=image_ids, profile=sender, direct_message__isnull=True).update(direct_message=message)
        if attached:
            from urbanlens.dashboard.models.direct_messages.image_permission import DirectMessageImagePermission

            # First image ever from this sender to this recipient starts the
            # consent handshake (blurred + Allow/Allow Once/Reject) - later
            # images just read whatever standing decision this row holds.
            DirectMessageImagePermission.objects.get_or_create(viewer=recipient, sender=sender)

    if markup_map is not None:
        from urbanlens.dashboard.services.map_sharing import share_markup_map_with_profile

        share_markup_map_with_profile(sender, recipient, markup_map)

    if body:
        # Typed coordinates/addresses are shares of those places and count in
        # the sender's reshare chain (see services.dm_location_detection).
        # Coordinates are regex + DB only, so they run inline (the broadcast
        # below then carries `has_location_mentions`); addresses need forward
        # geocoding, so they go to a Celery task.
        from urbanlens.dashboard.services.dm_location_detection import detect_coordinate_mentions, parse_addresses

        try:
            detect_coordinate_mentions(message)
        except DatabaseError:
            logger.exception("Coordinate detection failed for message %s", message.pk)
        if parse_addresses(body):
            from urbanlens.dashboard.tasks import detect_dm_address_mentions

            transaction.on_commit(lambda: detect_dm_address_mentions.delay(message.pk))

    # DEBUG, not INFO: this fires on every single message sent site-wide, so at
    # INFO it drowns out genuinely noteworthy log lines in production. Still
    # available on demand by turning DEBUG on, unlike removing it outright.
    logger.debug("Direct message %s: profile %s -> profile %s", message.pk, sender.pk, recipient.pk)

    if is_thread_open(recipient.pk, sender.pk):
        # Recipient is already looking at this conversation - mark it read
        # immediately and skip the notification/email entirely (see the
        # "open thread" tracking above, updated by the client on each WS
        # connect/thread-switch).
        DirectMessage.objects.filter(pk=message.pk).update(read_at=timezone.now())
        message.read_at = timezone.now()
    else:
        _notify_recipient(message)
        _schedule_message_email(message)

    if not defer_broadcast:
        _broadcast_direct_message(message)
    return message


def broadcast_direct_message(message: DirectMessage) -> None:
    """Push `message` to both participants' live sessions now.

    The public entry point for callers that passed ``defer_broadcast=True`` to
    `create_direct_message` (e.g. `services.direct_message_shares`, which
    attaches a `DirectMessageShare` before the message is visible on the wire).

    Args:
        message: The message to broadcast.
    """
    _broadcast_direct_message(message)


def _broadcast_message_update(payload: dict[str, Any], groups: set[str]) -> None:
    """Push an arbitrary JSON payload to the given channel-layer groups after commit.

    Shared by delete/tombstone updates - reuses the ``dm.message`` relay (the
    consumer forwards any payload verbatim), varying only the inner ``type``
    field so the client dispatches it correctly.

    Args:
        payload: JSON-serializable dict to deliver.
        groups: Channel-layer group names to deliver to.
    """

    def _send() -> None:
        layer = get_channel_layer()
        if layer is None:
            return
        for group in groups:
            try:
                async_to_sync(layer.group_send)(group, {"type": "dm.message", "message": payload})
            except Exception:
                logger.warning("Live push of message update to %s failed", group, exc_info=True)

    transaction.on_commit(_send)


def delete_message_for_everyone(message: DirectMessage, actor: Profile) -> DirectMessage:
    """Delete `message` for both participants - only the sender may do this.

    The sender's own view is unaffected (they always see their own messages -
    see the module docstring's consent policy); only the recipient's view
    switches to a tombstone. Revokes any pending `@`-mention share attached to
    the message, since deleting it removes the offer to act on it.

    Args:
        message: The message to delete.
        actor: The profile requesting the delete.

    Returns:
        The updated message.

    Raises:
        PermissionError: If `actor` isn't the message's sender.
    """
    if actor.pk != message.sender_id:
        raise PermissionError("Only the sender can delete this message for everyone.")
    if message.deleted_by_sender_at is None:
        message.deleted_by_sender_at = timezone.now()
        message.save(update_fields=["deleted_by_sender_at"])
        share = getattr(message, "share", None)
        if share is not None:
            share.revoke()
        _broadcast_message_update(
            {"type": "message_deleted", "message_id": message.pk, "scope": "everyone"},
            {direct_message_group_name(message.sender_id), direct_message_group_name(message.recipient_id)},
        )
    return message


def delete_message_for_self(message: DirectMessage, actor: Profile) -> DirectMessage:
    """Hide `message` from `actor`'s own view only - only the recipient may do this.

    The sender's copy and the underlying share (if any) are completely
    unaffected; this only changes what the recipient sees.

    Args:
        message: The message to hide.
        actor: The profile requesting the hide (must be the recipient).

    Returns:
        The updated message.

    Raises:
        PermissionError: If `actor` isn't the message's recipient.
    """
    if actor.pk != message.recipient_id:
        raise PermissionError("Only the recipient can remove this message from their own view.")
    if message.deleted_by_recipient_at is None:
        message.deleted_by_recipient_at = timezone.now()
        message.save(update_fields=["deleted_by_recipient_at"])
        _broadcast_message_update(
            {"type": "message_deleted", "message_id": message.pk, "scope": "self"},
            {direct_message_group_name(actor.pk)},
        )
    return message


def reaction_summary(message: DirectMessage) -> list[dict[str, Any]]:
    """Summarize a message's reactions grouped by emoji.

    Args:
        message: The message whose reactions to summarize.

    Returns:
        A list of ``{"emoji", "count", "slugs"}`` dicts, one per distinct
        emoji used. ``slugs`` lists the reacting profiles' slugs so the client
        can tell whether the viewer is among them.

    Note:
        Reads ``message.reactions.all()`` - callers rendering many messages
        should ``prefetch_related("reactions__profile")`` beforehand to avoid
        N+1 queries; this function itself never forces a fresh query.
    """
    grouped: dict[str, list[str]] = {}
    for reaction in message.reactions.all():
        grouped.setdefault(reaction.emoji, []).append(reaction.profile.slug or "")
    return [{"emoji": emoji, "count": len(slugs), "slugs": slugs} for emoji, slugs in grouped.items()]


def _broadcast_reaction(message: DirectMessage) -> None:
    """Push an updated reaction summary for `message` to both participants' live sessions.

    Args:
        message: The message whose reactions changed.
    """
    payload = {"type": "reaction", "message_id": message.pk, "reactions": reaction_summary(message)}
    groups = {direct_message_group_name(message.sender_id), direct_message_group_name(message.recipient_id)}

    def _send() -> None:
        layer = get_channel_layer()
        if layer is None:
            return
        for group in groups:
            try:
                async_to_sync(layer.group_send)(group, {"type": "dm.reaction", "message": payload})
            except Exception:
                logger.warning("Live push of reaction on message %s to %s failed", message.pk, group, exc_info=True)

    transaction.on_commit(_send)


def toggle_reaction(profile: Profile, message: DirectMessage, emoji: str) -> str:
    """Add or remove `profile`'s reaction of `emoji` on `message`.

    Args:
        profile: The reacting profile - must be the sender or recipient.
        message: The message being reacted to.
        emoji: The emoji character(s) to toggle.

    Returns:
        ``"added"`` or ``"removed"``.

    Raises:
        PermissionError: If `profile` isn't a participant in this message's conversation.
    """
    from urbanlens.dashboard.models.reactions.model import Reaction

    if profile.pk not in (message.sender_id, message.recipient_id):
        raise PermissionError("You aren't part of this conversation.")

    existing = Reaction.objects.existing(profile, emoji, direct_message=message)
    if existing:
        existing.delete()
        action = "removed"
    else:
        Reaction.objects.create(profile=profile, direct_message=message, emoji=emoji)
        action = "added"
    _broadcast_reaction(message)
    return action


def display_identity_for(viewer: Profile, partner: Profile) -> dict[str, Any]:
    """Return how `partner` should be displayed to `viewer` in a DM context right now.

    A past conversation stays fully readable even after `partner`'s privacy
    settings or friendship status change to no longer permit `viewer` to view
    their profile - but their identity is anonymized everywhere it would
    otherwise be shown (name, avatar, profile link), since `viewer` no longer
    has standing access to know who they are.

    Args:
        viewer: The profile viewing the conversation.
        partner: The conversation partner whose identity is being displayed.

    Returns:
        Dict with ``display_name``, ``display_avatar_url`` (str or None),
        ``display_profile_url`` (str or None), and ``is_anonymized`` (bool).
    """
    if partner.can_view_profile(viewer):
        from django.urls import reverse

        return {
            "display_name": partner.username,
            "display_avatar_url": partner.avatar.url if partner.avatar else None,
            "display_profile_url": reverse("profile.view_user", kwargs={"profile_slug": partner.slug}) if partner.slug else None,
            "is_anonymized": False,
        }
    return {
        "display_name": "Former contact",
        "display_avatar_url": None,
        "display_profile_url": None,
        "is_anonymized": True,
    }


def conversations_for(profile: Profile) -> list[dict[str, Any]]:
    """Return the profile's conversations, most recently active first.

    Args:
        profile: The profile whose inbox to build.

    Returns:
        A list of dicts with ``partner`` (Profile), ``last_message``
        (DirectMessage), ``unread_count`` (int), and the
        ``display_identity_for`` keys for rendering the partner's identity.
    """
    from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    rows = list(DirectMessage.objects.conversation_rows(profile))
    if not rows:
        return []

    partners = ProfileModel.objects.select_related("user").in_bulk([row["partner_id"] for row in rows])
    last_messages = DirectMessage.objects.in_bulk([row["last_message_id"] for row in rows])
    muted_sender_ids = set(DirectMessageMute.objects.filter(viewer=profile).values_list("sender_id", flat=True))

    conversations = []
    for row in rows:
        partner = partners.get(row["partner_id"])
        last_message = last_messages.get(row["last_message_id"])
        if partner is None or last_message is None:
            continue
        conversations.append(
            {
                "kind": "dm",
                "partner": partner,
                "last_message": last_message,
                "unread_count": row["unread_count"],
                "is_muted": partner.pk in muted_sender_ids,
                "last_activity": last_message.created,
                **display_identity_for(profile, partner),
            },
        )
    return conversations


def all_conversations_for(profile: Profile) -> list[dict[str, Any]]:
    """Return the profile's one-to-one and group conversations merged, newest first.

    Args:
        profile: The profile whose inbox to build.

    Returns:
        Dicts from :func:`conversations_for` (``kind="dm"``) and
        :func:`~urbanlens.dashboard.services.group_chats.group_conversations_for`
        (``kind="group"``), sorted by last activity.
    """
    from urbanlens.dashboard.services.group_chats import group_conversations_for

    merged = conversations_for(profile) + group_conversations_for(profile)
    merged.sort(key=lambda conv: conv["last_activity"], reverse=True)
    return merged


#: Consecutive messages from the same sender closer together than this are
#: visually grouped (tighter spacing, softened corner) instead of rendered as
#: separate full-weight bubbles - matches the "message burst" treatment other
#: chat apps use. Also the window Slack uses for its own message grouping.
MESSAGE_GROUP_GAP_SECONDS = 300


def key_change_events_for(profile: Profile, partner: Profile) -> list[dict[str, Any]]:
    """Return this pair's encryption-key rotations after their first key.

    The very first ``ConversationKey`` (version 1) is just the conversation
    getting encrypted for the first time, not a "change" worth flagging;
    every later version is a reset (recovery-key reset, "forgot my password",
    etc.) that other E2EE chat apps surface as a system notice, since it means
    messages from here on are secured under a different key.

    Args:
        profile: One participant.
        partner: The other participant.

    Returns:
        Dicts with ``kind="key_change"``, ``created``, and ``version``, one
        per rotation, oldest first.
    """
    from urbanlens.dashboard.models.e2ee.conversation_key import ConversationKey

    low, high = ConversationKey.canonical_pair(profile, partner)
    rows = ConversationKey.objects.filter(profile_low=low, profile_high=high, version__gt=1).order_by("version")
    return [{"kind": "key_change", "created": row.created, "version": row.version} for row in rows]


def thread_page(profile: Profile, partner: Profile, *, before_id: int | None = None, limit: int = THREAD_PAGE_SIZE) -> tuple[list[DirectMessage], bool]:
    """Return one page of a conversation, most recent messages first page by default.

    Pages are keyed off ``id`` rather than ``created`` - equivalent ordering
    for this insert-only, auto-incrementing table, but ``id`` is what the
    "load older history" cursor (``before_id``) needs to filter on.

    Args:
        profile: One participant.
        partner: The other participant.
        before_id: When given, only messages with a smaller pk are considered
            (paginating further into the past); None loads the most recent page.
        limit: Maximum number of messages to return.

    Returns:
        A tuple of ``(messages, has_more_older)``: messages oldest-first,
        ready to render in a timeline; ``has_more_older`` is True when at
        least one older message exists beyond this page.
    """
    queryset = (
        DirectMessage.objects.between(profile, partner)
        .select_related(
            "sender",
            "sender__user",
            "recipient",
            "recipient__user",
            "reply_to",
            "reply_to__sender",
            "share",
            "share__pin_share",
            "share__pin_share__pin",
            "share__trip",
            "share__trip_membership",
            "share__recommended_profile",
            "markup_map",
        )
        .prefetch_related("images", "reactions__profile", "markup_map__items", "location_mentions__location", "location_mentions__pin_share")
    )
    if before_id is not None:
        queryset = queryset.filter(pk__lt=before_id)
    page = list(queryset.order_by("-id")[: limit + 1])
    has_more_older = len(page) > limit
    page = page[:limit]
    page.reverse()
    return page, has_more_older


def build_thread_timeline(messages: list[DirectMessage], key_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge messages and key-change notices into one chronological timeline.

    Also annotates each message with ``is_grouped`` (see
    ``MESSAGE_GROUP_GAP_SECONDS``): True when it directly follows another
    message from the same sender, on the same day, with no key-change notice
    between them, *and* that previous message wasn't already read before this
    one was even sent - the template uses this to render message bursts
    tighter together instead of as a string of identical, evenly-spaced
    bubbles. That last condition matters for a burst spanning a "catch up"
    moment: if the recipient had already read message N in an earlier visit
    and message N+1 only arrived later, grouping them tightly would hide the
    fact that N+1 is new since they last looked, so the group breaks there
    instead. This only runs against the server-rendered history (full loads,
    conversation switches, the socket-down POST fallback) - live messages
    appended client-side over the websocket use their own, simpler grouping
    check (sender + elapsed time only) precisely to avoid this rule firing
    mid-conversation: with the thread open, an incoming message is marked
    read almost immediately (see ``is_thread_open``), which would otherwise
    break apart every live back-and-forth burst as it streams in.

    Args:
        messages: Thread messages, oldest first (mutated in place: each gets
            an ``is_grouped`` attribute).
        key_events: Rows from ``key_change_events_for``.

    Returns:
        Dicts ordered by ``created``, each either ``{"kind": "message",
        "created", "message"}`` or a key-change dict from ``key_events``.
    """
    items: list[dict[str, Any]] = [{"kind": "message", "created": message.created, "message": message} for message in messages]
    items.extend(key_events)
    items.sort(key=lambda item: item["created"])

    previous: DirectMessage | None = None
    for item in items:
        if item["kind"] != "message":
            previous = None
            continue
        message = item["message"]
        already_read_before_sent = bool(previous and previous.read_at and previous.read_at <= message.created)
        message.is_grouped = bool(
            previous and previous.sender_id == message.sender_id and previous.created.date() == message.created.date() and (message.created - previous.created).total_seconds() < MESSAGE_GROUP_GAP_SECONDS and not already_read_before_sent,
        )
        previous = message
    return items


def has_used_direct_messages(profile: Profile) -> bool:
    """Return True when the profile has ever sent or received a direct message.

    Gates the navbar messages icon: users who have never touched the feature
    don't get an extra icon competing for attention.

    Args:
        profile: The profile to check.

    Returns:
        True when at least one message or group chat involves the profile.
    """
    from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

    return DirectMessage.objects.involving(profile).exists() or GroupChatMembership.objects.active().filter(profile=profile).exists()


#: Default result cap for the Messages page's own search (as opposed to
#: global search's smaller per-section limit, since this is the only section
#: rendered here).
DIRECT_MESSAGE_SEARCH_LIMIT = 25


def message_search_queryset(profile: Profile, parsed: ParsedQuery, *, partner: Profile | None = None) -> QuerySet[DirectMessage]:
    """Build the filtered, ordered DirectMessage queryset for a parsed search query.

    Shared by the global Ctrl+K search's ``DirectMessageSearchProvider`` and
    the Messages page's own "search this conversation" / "search all
    conversations" features, so visibility, date-range, and person-name rules
    can never drift between the two surfaces. Only plaintext bodies are
    searchable - end-to-end encrypted messages never reach the server in
    readable form, so they are excluded outright rather than silently
    mismatched.

    Args:
        profile: The searching profile; results are scoped to messages they
            sent or received, minus anything they've deleted for themselves.
        parsed: The parsed query (free-text terms, date range, person name).
        partner: Restrict to the conversation with this partner ("search this
            conversation"); None searches every conversation.

    Returns:
        Matching messages, most recent first.
    """
    from urbanlens.dashboard.services.global_search.providers import date_range_filter, person_match, term_filter

    queryset = (DirectMessage.objects.between(profile, partner) if partner is not None else DirectMessage.objects.involving(profile)).visible_to(profile).exclude(body="")
    queryset = queryset.filter(date_range_filter("created", parsed))
    if parsed.person:
        recipient_ann, recipient_q = person_match("recipient", parsed.person, profile)
        sender_ann, sender_q = person_match("sender", parsed.person, profile)
        queryset = queryset.annotate(**recipient_ann, **sender_ann).filter((Q(sender=profile) & recipient_q) | (Q(recipient=profile) & sender_q))
    if parsed.terms:
        queryset = queryset.filter(term_filter(parsed.terms, ["body"]))
    return queryset.order_by("-created")


def search_direct_messages(profile: Profile, raw_query: str, *, partner: Profile | None = None, limit: int = DIRECT_MESSAGE_SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Search the profile's direct messages using the same NL parser as global search.

    Understands the same phrasing global search does - "photos last week",
    "between june 1 and june 10", "from Alice" - by reusing
    :func:`~urbanlens.dashboard.services.global_search.parser.parse_query` and
    :func:`message_search_queryset` rather than a second, DM-page-specific
    parser.

    Args:
        profile: The searching profile.
        raw_query: The query exactly as typed.
        partner: Restrict to one conversation ("search this conversation");
            None searches every conversation ("search all conversations").
        limit: Maximum number of hits to return.

    Returns:
        Dicts with ``message`` (DirectMessage), ``partner`` (the other
        participant on that message), ``snippet`` (excerpt around the match),
        and ``url`` (link to the conversation, anchored to the message),
        most recent match first. Empty when the query carries no usable
        signal (blank, or below global search's minimum length/structure).
    """
    from django.urls import reverse

    from urbanlens.dashboard.services.global_search.parser import parse_query
    from urbanlens.dashboard.services.global_search.results import excerpt

    parsed = parse_query(raw_query)
    if parsed.is_empty:
        return []

    queryset = message_search_queryset(profile, parsed, partner=partner).select_related("sender__user", "recipient__user")[:limit]
    hits = []
    for message in queryset:
        other = message.recipient if message.sender_id == profile.pk else message.sender
        hits.append(
            {
                "message": message,
                "partner": other,
                "snippet": excerpt(message.body, parsed.terms),
                "url": f"{reverse('messages.conversation', kwargs={'profile_slug': other.ensure_slug()})}#dm-msg-{message.pk}",
            },
        )
    return hits
