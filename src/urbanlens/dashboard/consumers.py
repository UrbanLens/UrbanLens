import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


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
    """

    async def connect(self):
        """Resolve the check-in (and, on the contact route, the authorizing contact), then join its group."""
        from django.core.exceptions import ObjectDoesNotExist

        kwargs = self.scope["url_route"]["kwargs"]
        try:
            self.checkin, self.contact = await self._resolve(kwargs.get("checkin_uuid"), kwargs.get("token"))
        except (ObjectDoesNotExist, PermissionError):
            await self.close(code=4404)
            return

        self.group_name = f"safety_checkin_{self.checkin.pk}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        """Leave the check-in's group, if we ever joined one."""
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        """Persist an incoming chat message and broadcast it to the check-in's group.

        Args:
            text_data: JSON string with a ``body`` field. Silently ignored if
                unparseable or blank - there's nothing useful to tell the
                client via a plain WebSocket text frame.
        """
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return
        body = str(data.get("body") or "").strip()
        if not body:
            return

        message = await self._create_message(body)
        await self.channel_layer.group_send(self.group_name, {"type": "chat.message", "message": message})

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
            "id": message.pk,
            "sender_name": message.sender_name,
            "body": message.body,
            "created": message.created.isoformat(),
        }
