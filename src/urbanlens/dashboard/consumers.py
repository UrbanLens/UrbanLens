import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class UserNotificationConsumer(AsyncWebsocketConsumer):
    """Pushes on-site notifications to a logged-in user's open tabs as they are created.

    Mounted at ``ws/notifications/``. Authentication comes from the session
    cookie via Channels' ``AuthMiddlewareStack``; each connection joins the
    per-profile group produced by
    ``urbanlens.dashboard.models.notifications.signals.notification_group_name``,
    which the ``NotificationLog`` post_save signal broadcasts to.

    The socket is strictly server → client: incoming frames are ignored, and
    marking notifications read stays on the existing HTMX endpoints.

    Close codes on ``connect()`` failure (mirroring ``SafetyCheckinChatConsumer``):

    - ``4404``: the session is unauthenticated - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.
    """

    async def connect(self):
        """Authenticate the session and join the profile's notification group."""
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4404)
            return

        try:
            profile_id = await self._get_profile_id()
            from urbanlens.dashboard.models.notifications.signals import notification_group_name

            self.group_name = notification_group_name(profile_id)
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
        except Exception:
            logger.exception("Notification socket connect failed for user %s", getattr(user, "pk", None))
            await self.close(code=4500)

    async def disconnect(self, close_code):
        """Leave the notification group, if we ever joined one."""
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Notification socket failed to leave group %s cleanly", self.group_name)

    async def receive(self, text_data):
        """Ignore client frames - this socket is server → client only."""

    async def notification_new(self, event):
        """Deliver one broadcasted notification to this connection.

        Args:
            event: The group-send event, with a ``notification`` dict payload.
        """
        await self.send(text_data=json.dumps({"type": "notification", "notification": event["notification"]}))

    @database_sync_to_async
    def _get_profile_id(self):
        """Resolve (creating if needed) the session user's profile id.

        Returns:
            The primary key of the user's Profile.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        profile, _ = Profile.objects.get_or_create(user=self.scope["user"])
        return profile.pk


class DirectMessageConsumer(AsyncWebsocketConsumer):
    """Real-time direct-message channel for a logged-in user.

    Mounted at ``ws/messages/``. Authentication comes from the session cookie
    via Channels' ``AuthMiddlewareStack``. Each connection joins the
    per-profile group from ``services.direct_messages.direct_message_group_name``;
    ``create_direct_message`` broadcasts every new message to both the sender's
    and the recipient's groups, so all of either party's open tabs update at once.

    Sending: the client submits ``{"recipient": "<profile slug>", "body": "..."}``
    frames; validation, privacy enforcement, persistence, and the broadcast all
    live in ``create_direct_message`` - the same function the HTTP fallback
    (``ConversationSendView``) uses, mirroring the safety check-in chat split.

    Close codes on ``connect()`` failure (same contract as
    ``SafetyCheckinChatConsumer``, which the frontend reconnect logic branches on):

    - ``4404``: the session is unauthenticated - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.
    """

    async def connect(self):
        """Authenticate the session, join the profile's direct-message group, and mark them online."""
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4404)
            return

        try:
            self.profile_id = await self._get_profile_id()
            from urbanlens.dashboard.services.direct_messages import direct_message_group_name, mark_profile_online

            self.group_name = direct_message_group_name(self.profile_id)
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            await database_sync_to_async(mark_profile_online)(self.profile_id)
        except Exception:
            logger.exception("Direct message socket connect failed for user %s", getattr(user, "pk", None))
            await self.close(code=4500)

    async def disconnect(self, close_code):
        """Leave the direct-message group and mark one fewer live connection, if we ever joined."""
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Direct message socket failed to leave group %s cleanly", self.group_name)
        if hasattr(self, "profile_id"):
            from urbanlens.dashboard.services.direct_messages import mark_profile_offline

            try:
                await database_sync_to_async(mark_profile_offline)(self.profile_id)
            except Exception:
                logger.exception("Direct message socket failed to mark profile %s offline", self.profile_id)

    async def receive(self, text_data):
        """Persist an incoming message; the service broadcasts it to both parties.

        Args:
            text_data: JSON string with ``recipient`` (profile slug), ``body``,
                and optional ``image_ids``/``markup_map_uuid``/``reply_to``
                fields. Unparseable frames, or frames with no recipient and no
                body/attachment at all, are silently ignored; a frame that
                fails validation or privacy checks gets an explicit
                ``{"type": "error", ...}`` reply so the sender always learns
                their message didn't go through.
        """
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Direct message socket received an unparseable frame from profile %s", self.profile_id)
            return

        if data.get("type") == "typing":
            recipient_slug = str(data.get("recipient") or "").strip()
            if recipient_slug:
                from urbanlens.dashboard.services.direct_messages import broadcast_typing_indicator

                await database_sync_to_async(broadcast_typing_indicator)(self.profile_id, recipient_slug)
            return

        if data.get("type") == "open":
            recipient_slug = str(data.get("recipient") or "").strip()
            group_uuid = str(data.get("group") or "").strip()
            if group_uuid:
                await self._mark_group_thread_open(group_uuid)
            elif recipient_slug:
                await self._mark_thread_open(recipient_slug)
            return

        body = str(data.get("body") or "").strip()
        ciphertext = str(data.get("ciphertext") or "").strip()
        nonce = str(data.get("nonce") or "").strip()
        key_version = data.get("key_version")
        key_version = int(key_version) if isinstance(key_version, int) else 0

        group_uuid = str(data.get("group") or "").strip()
        if group_uuid:
            # A group-chat frame: same validation/broadcast pipeline, but the
            # message fans out to every active member (see services.group_chats).
            if not (body or ciphertext):
                return
            try:
                await self._create_group_message(group_uuid, body, ciphertext, nonce, key_version)
            except (ValueError, PermissionError) as exc:
                await self.send(text_data=json.dumps({"type": "error", "detail": str(exc)}))
            except Exception:
                logger.exception("Group message failed to save from profile %s", self.profile_id)
                await self.send(text_data=json.dumps({"type": "error", "detail": "Your message couldn't be sent. Please try again."}))
            return

        recipient_slug = str(data.get("recipient") or "").strip()
        image_ids = [int(v) for v in data.get("image_ids") or [] if isinstance(v, int)]
        markup_map_uuid = str(data.get("markup_map_uuid") or "").strip() or None
        reply_to_id = data.get("reply_to")
        reply_to_id = int(reply_to_id) if isinstance(reply_to_id, int) else None
        if not recipient_slug or not (body or ciphertext or image_ids or markup_map_uuid):
            return

        try:
            await self._create_message(recipient_slug, body, ciphertext, nonce, key_version, image_ids, markup_map_uuid, reply_to_id)
        except (ValueError, PermissionError) as exc:
            await self.send(text_data=json.dumps({"type": "error", "detail": str(exc)}))
        except Exception:
            logger.exception("Direct message failed to save from profile %s", self.profile_id)
            await self.send(text_data=json.dumps({"type": "error", "detail": "Your message couldn't be sent. Please try again."}))

    async def dm_message(self, event):
        """Deliver a broadcasted message to this connection.

        Args:
            event: The group-send event, with a ``message`` dict payload.
        """
        await self.send(text_data=json.dumps(event["message"]))

    async def dm_reaction(self, event):
        """Deliver a broadcasted reaction-summary update to this connection.

        Args:
            event: The group-send event, with a ``message`` dict payload
                (``{"type": "reaction", "message_id": ..., "reactions": [...]}``).
        """
        await self.send(text_data=json.dumps(event["message"]))

    @database_sync_to_async
    def _mark_group_thread_open(self, group_uuid):
        """Record that this connection currently has a group thread open.

        Args:
            group_uuid: UUID string of the group chat being viewed.
        """
        from urbanlens.dashboard.models.group_chats.model import GroupChat
        from urbanlens.dashboard.services.group_chats import mark_group_thread_open

        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None:
            return
        mark_group_thread_open(self.profile_id, group.pk)

    @database_sync_to_async
    def _create_group_message(self, group_uuid, body, ciphertext, nonce, key_version):
        """Resolve the group and create the message through the shared service.

        Args:
            group_uuid: UUID string of the target group chat.
            body: Plaintext message text (blank for encrypted messages).
            ciphertext: End-to-end encrypted body (base64), or blank.
            nonce: Base64 nonce for ``ciphertext``.
            key_version: GroupKey version that encrypted this message.

        Raises:
            ValueError: Bad content or no such group.
            PermissionError: The sender isn't an active member.
        """
        from urbanlens.dashboard.models.group_chats.model import GroupChat
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.group_chats import create_group_message

        sender = Profile.objects.select_related("user").get(pk=self.profile_id)
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None:
            raise ValueError("That group could not be found.")
        create_group_message(sender, group, body, ciphertext=ciphertext, nonce=nonce, key_version=key_version)

    @database_sync_to_async
    def _mark_thread_open(self, recipient_slug):
        """Record that this connection currently has the thread with `recipient_slug` open.

        Args:
            recipient_slug: URL slug of the conversation partner being viewed.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.direct_messages import mark_thread_open

        try:
            partner = Profile.objects.get(slug=recipient_slug)
        except Profile.DoesNotExist:
            return
        mark_thread_open(self.profile_id, partner.pk)

    @database_sync_to_async
    def _get_profile_id(self):
        """Resolve (creating if needed) the session user's profile id.

        Returns:
            The primary key of the user's Profile.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        profile, _ = Profile.objects.get_or_create(user=self.scope["user"])
        return profile.pk

    @database_sync_to_async
    def _create_message(self, recipient_slug, body, ciphertext, nonce, key_version, image_ids, markup_map_uuid, reply_to_id):
        """Resolve the recipient and create the message through the shared service.

        Args:
            recipient_slug: URL slug of the recipient profile.
            body: Plaintext message text (blank for encrypted messages).
            ciphertext: End-to-end encrypted body (base64), or blank.
            nonce: Base64 nonce for ``ciphertext``.
            key_version: ConversationKey version that encrypted this message.
            image_ids: PKs of the sender's unattached Image rows to attach.
            markup_map_uuid: UUID of a MarkupMap owned by the sender to attach.
            reply_to_id: PK of an earlier message in this conversation to quote.

        Raises:
            ValueError: Blank/too-long/malformed content, or no such recipient.
            PermissionError: The recipient's privacy settings reject the sender.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.direct_messages import create_direct_message

        sender = Profile.objects.select_related("user").get(pk=self.profile_id)
        try:
            recipient = Profile.objects.select_related("user").get(slug=recipient_slug)
        except Profile.DoesNotExist:
            raise ValueError("That user could not be found.") from None
        create_direct_message(
            sender,
            recipient,
            body,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            image_ids=image_ids,
            markup_map_uuid=markup_map_uuid,
            reply_to_id=reply_to_id,
        )


class SafetyCheckinChatConsumer(AsyncWebsocketConsumer):
    """Real-time chat for a safety check-in, shared by the owner and every emergency contact.

    Mounted under two routes (see ``dashboard/routing.py``):

    - ``ws/safety/checkin/<uuid:checkin_uuid>/chat/`` - owner route, requires
      an authenticated session that owns the check-in (populated by
      Channels' ``AuthMiddlewareStack`` from the session cookie).
    - ``ws/safety/contact/<uuid:token>/chat/`` - contact route, authorized by
      the token alone (mirrors ``SafetyCheckinMessageView._resolve`` - a
      contact identified only by email has no account to log into).

    Everyone connected for a given check-in - the owner and all of its
    contacts - joins the same channel group, so a message from any one of
    them is broadcast to all the others immediately.

    Close codes used on ``connect()`` failure (the frontend branches on these
    to decide whether to keep retrying):

    - ``4404``: the check-in/contact/token doesn't resolve, or the owner
      route was hit while unauthenticated - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.

    Incoming frames that fail to save are answered with
    ``{"type": "error", "detail": "..."}`` rather than silently dropped or
    left to crash the socket, so a sender always learns their message didn't
    go through - important for a feature people may rely on in an emergency.
    """

    async def connect(self):
        """Resolve the check-in (and, on the contact route, the authorizing contact), then join its group."""
        from django.core.exceptions import ObjectDoesNotExist

        kwargs = self.scope["url_route"]["kwargs"]
        try:
            self.checkin, self.contact = await self._resolve(kwargs.get("checkin_uuid"), kwargs.get("token"))
        except (ObjectDoesNotExist, PermissionError):
            logger.info("Safety chat connection rejected (not found/unauthorized): %s", kwargs)
            await self.close(code=4404)
            return
        except Exception:
            logger.exception("Safety chat connect failed unexpectedly: %s", kwargs)
            await self.close(code=4500)
            return

        self.group_name = f"safety_checkin_{self.checkin.pk}"
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
        except Exception:
            logger.exception("Safety chat failed to join group for checkin %s", self.checkin.pk)
            await self.close(code=4500)
            return
        logger.info("Safety chat connected: checkin=%s contact=%s", self.checkin.pk, getattr(self.contact, "pk", None))

    async def disconnect(self, close_code):
        """Leave the check-in's group, if we ever joined one."""
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Safety chat failed to leave group %s cleanly", self.group_name)

    async def receive(self, text_data):
        """Persist an incoming chat message and broadcast it to the check-in's group.

        Args:
            text_data: JSON string with a ``body`` field. Unparseable or
                blank frames are silently ignored - there's no message to
                report failure for. A frame that fails validation or fails
                to save gets an explicit ``{"type": "error", ...}`` reply
                instead.
        """
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Safety chat received an unparseable frame on checkin %s", self.checkin.pk)
            return
        body = str(data.get("body") or "").strip()
        if not body:
            return

        try:
            message = await self._create_message(body)
        except ValueError as exc:
            await self.send(text_data=json.dumps({"type": "error", "detail": str(exc)}))
            return
        except Exception:
            logger.exception("Safety chat message failed to save on checkin %s", self.checkin.pk)
            await self.send(text_data=json.dumps({"type": "error", "detail": "Your message couldn't be sent. Please try again."}))
            return

        try:
            await self.channel_layer.group_send(self.group_name, {"type": "chat.message", "message": message})
        except Exception:
            # The message is already saved - only the live broadcast failed (e.g. a
            # transient channel-layer/Valkey hiccup). Tell the sender so they don't
            # think it vanished; other participants will still see it on next load.
            logger.exception("Safety chat broadcast failed on checkin %s", self.checkin.pk)
            await self.send(text_data=json.dumps({"type": "error", "detail": "Your message was saved but couldn't be delivered live. It'll appear on refresh."}))

    async def chat_message(self, event):
        """Deliver a broadcasted message to this connection.

        Args:
            event: The group-send event, with a ``message`` dict payload.
        """
        await self.send(text_data=json.dumps(event["message"]))

    async def status_update(self, event):
        """Deliver a check-in status change (escalated/found-safe) to this connection.

        Args:
            event: The group-send event, with a ``payload`` dict (see
                ``services.safety._broadcast_status_update``).
        """
        await self.send(text_data=json.dumps(event["payload"]))

    @database_sync_to_async
    def _resolve(self, checkin_uuid, token):
        """Resolve the check-in and, for the contact route, the authorizing contact.

        Args:
            checkin_uuid: UUID of the check-in (owner route).
            token: Contact's magic-link token (contact route).

        Returns:
            (checkin, contact) - contact is None on the owner route.

        Raises:
            ObjectDoesNotExist: If the check-in/contact/token doesn't resolve.
            PermissionError: If the owner route is used while unauthenticated.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact

        if token is not None:
            contact = SafetyCheckinContact.objects.select_related("checkin").get(token=token)
            return contact.checkin, contact

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            raise PermissionError
        profile, _ = Profile.objects.get_or_create(user=user)
        checkin = SafetyCheckin.objects.get(uuid=checkin_uuid, profile=profile)
        return checkin, None

    @database_sync_to_async
    def _create_message(self, body):
        """Create the chat message and serialize it for broadcast.

        Args:
            body: Message text.

        Returns:
            A JSON-serializable dict describing the new message.
        """
        from django.contrib.auth.models import AnonymousUser

        from urbanlens.dashboard.services.safety import create_chat_message

        user = self.scope.get("user") or AnonymousUser()
        message = create_chat_message(self.checkin, user=user, contact=self.contact, body=body)
        return {
            "type": "message",
            "id": message.pk,
            "sender_name": message.sender_name,
            "body": message.body,
            "created": message.created.isoformat(),
        }
