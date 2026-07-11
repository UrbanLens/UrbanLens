import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class RequestStatusConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = "request_status"
        self.room_group_name = f"updates_{self.room_name}"

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        pass

    async def send_status(self, event):
        message = event["message"]
        await self.send(text_data=json.dumps({"message": message}))


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
