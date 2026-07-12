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
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Common emoji offered by the quick "add a reaction" picker on each message.
REACTION_PICKER_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉"]


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
        "markup_map_uuid": str(message.markup_map.uuid) if message.markup_map is not None else None,
        "reply_to": reply_to,
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

    if message.is_encrypted:
        preview = "🔒 Encrypted message"
    else:
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
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

    try:
        pref = message.recipient.notification_preferences.message
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref not in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
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

    logger.info("Direct message %s: profile %s -> profile %s", message.pk, sender.pk, recipient.pk)

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

    _broadcast_direct_message(message)
    return message


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

    existing = Reaction.objects.filter(profile=profile, direct_message=message, emoji=emoji).first()
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
                **display_identity_for(profile, partner),
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
