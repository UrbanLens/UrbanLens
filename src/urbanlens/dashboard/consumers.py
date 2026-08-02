from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from urbanlens.dashboard.websocket_auth import CREDENTIAL_SCOPE_KEY

if TYPE_CHECKING:
    # CredentialScopeMixin calls self.scope/self.close/self.send, which every
    # class it is mixed into inherits from AsyncWebsocketConsumer. Declaring that
    # base for the type checker only is what makes those calls check out without
    # a cast or a blanket ignore, while keeping the runtime MRO a plain mixin
    # (`class Consumer(CredentialScopeMixin, AsyncWebsocketConsumer)`) rather
    # than a second inheritance path into the consumer machinery.
    class _CredentialScopeBase(AsyncWebsocketConsumer): ...

else:

    class _CredentialScopeBase: ...


logger = logging.getLogger(__name__)

# How often SafetyCheckinChatConsumer re-validates a session-route connection's
# permission, as a backstop for a dropped partner_access_revoked broadcast. Frequent
# enough that a revoked partner's live-location stream can't leak for long; infrequent
# enough not to add meaningful DB load for what's normally a no-op check.
_PARTNER_REVALIDATION_INTERVAL_SECONDS = 60

# How often a credential-authenticated connection re-checks that its ApiKey/OAuth2
# token is still valid. Revoking a credential has to actually stop delivery, not just
# block the next HTTP call: a WebSocket authenticates once, at connect(), so without a
# periodic re-check a user who revokes a leaked key would watch it keep streaming their
# notifications and messages until the process restarts. Session connections never run
# this loop at all (they have no credential to revoke), so it costs the web client
# nothing.
_CREDENTIAL_REVALIDATION_INTERVAL_SECONDS = 60

#: Sent back when a credential-authenticated connection tries to write with a
#: read-only grant. A frame-level error rather than a close, matching how these
#: consumers already report a message that failed to save: closing would put the
#: client into a reconnect loop over a condition that retrying cannot fix.
_INSUFFICIENT_SCOPE_DETAIL = "This credential isn't allowed to send here. Reconnect with a credential granting the matching write scope."


def _credential_is_still_valid(credential: Any) -> bool:
    """Re-read *credential* from the database and report whether it can still authenticate.

    Both credential kinds are checked the way their own HTTP authenticator
    would check them on a fresh request, because that is the guarantee being
    reproduced - a socket must not outlive the authority that opened it:

    - a PAT-style ``ApiKey`` is revoked in place, by stamping ``revoked_at``
      (``services.auth.api_keys.revoke_api_key``), so a stamped row is dead;
    - a django-oauth-toolkit ``AccessToken`` is revoked by *deleting* the row,
      and separately stops working when it expires, so a missing row or an
      expired one is dead.

    Args:
        credential: The ``ApiKey``/``AccessToken`` resolved at connect time, or
            None for a session-authenticated connection.

    Returns:
        True when the connection may continue - including for None, since a
        session connection has no credential to revoke and its own separate
        checks (if any) are unaffected by this one.
    """
    if credential is None:
        return True
    refreshed = type(credential).objects.filter(pk=credential.pk).first()
    if refreshed is None:
        return False
    if getattr(refreshed, "revoked_at", None) is not None:
        return False
    is_expired = getattr(refreshed, "is_expired", None)
    return not (callable(is_expired) and is_expired())


class CredentialScopeMixin(_CredentialScopeBase):
    """Applies the external API's per-credential scope rules to a WebSocket consumer.

    Over HTTP, resolving an ``ApiKey``/OAuth2 token is only step one: every
    ``external_api`` view then runs
    :class:`~urbanlens.dashboard.external_api.permissions.HasApiKeyScope`, so a
    credential reaches only the domains its grant names. ``ApiKeyAuthMiddleware``
    gave sockets step one; this mixin gives them step two, using the *same*
    ``credential_grants`` function rather than a parallel implementation - a
    second copy of a scope check is a second thing that can silently become the
    more permissive one.

    Everything here keys off ``scope["api_credential"]``. A browser-session
    connection has None there and every method below is a no-op for it, which
    is the whole point: this must not change the web client's behavior in any
    way. It is the identical discriminator
    ``external_api.mixins.IsSessionAuthenticated`` uses on the HTTP side
    (``request.auth is None`` means "a session, not a credential").
    """

    @property
    def credential(self) -> Any:
        """The ``ApiKey``/``AccessToken`` that opened this connection, or None for a session.

        Read via ``scope.get`` so a consumer instantiated without
        ``ApiKeyAuthMiddleware`` in the stack (unit tests, any future ASGI
        entrypoint) degrades to the session path instead of raising KeyError
        and killing the socket.
        """
        return self.scope.get(CREDENTIAL_SCOPE_KEY)

    def credential_allows(self, *scopes: str) -> bool:
        """Whether this connection may exercise *scopes*.

        Args:
            *scopes: The :class:`~urbanlens.dashboard.models.account.model.ApiKeyScope`
                values required for the operation being attempted.

        Returns:
            True for a session connection (no credential, nothing to restrict)
            and for a credential granting every requested scope; False
            otherwise. Note that ``credential_grants`` independently refuses
            ``OAUTH2_ONLY_SCOPES`` to PAT-kind credentials, which is what keeps
            a ``ulk_`` key out of ``ws/messages/``.
        """
        from urbanlens.dashboard.external_api.permissions import credential_grants

        credential = self.credential
        if credential is None:
            return True
        # No database access happens here - an ApiKey's ``scopes`` list and an
        # AccessToken's ``scope`` string are both already loaded on the instance
        # the middleware resolved - so this is safe to call from the event loop
        # without a database_sync_to_async hop on every inbound frame.
        return credential_grants(credential, scopes)

    def start_credential_revalidation(self) -> None:
        """Begin periodically re-checking that this connection's credential is still valid.

        A no-op for session connections. Call once, after ``accept()``; pair it
        with :meth:`stop_credential_revalidation` in ``disconnect()`` so the
        task doesn't outlive the socket.
        """
        if self.credential is None:
            return
        self._credential_revalidation_task = asyncio.create_task(self._revalidate_credential_periodically())

    def stop_credential_revalidation(self) -> None:
        """Cancel the revalidation task, if one was ever started."""
        task = getattr(self, "_credential_revalidation_task", None)
        if task is not None:
            task.cancel()

    async def _revalidate_credential_periodically(self) -> None:
        """Close this connection with 4404 as soon as its credential stops being valid."""
        try:
            while True:
                await asyncio.sleep(_CREDENTIAL_REVALIDATION_INTERVAL_SECONDS)
                if not await self._credential_still_valid():
                    logger.info("Closing socket %s: its credential was revoked or expired", type(self).__name__)
                    await self.close(code=4404)
                    return
        except asyncio.CancelledError:
            pass

    @database_sync_to_async
    def _credential_still_valid(self) -> bool:
        """Re-read this connection's credential from the database - see :func:`_credential_is_still_valid`."""
        return _credential_is_still_valid(self.credential)


class UserNotificationConsumer(CredentialScopeMixin, AsyncWebsocketConsumer):
    """Pushes on-site notifications to a logged-in user's open tabs as they are created.

    Mounted at ``ws/notifications/``. Authentication comes from the session
    cookie via Channels' ``AuthMiddlewareStack``, or from a ``?key=``
    credential via ``ApiKeyAuthMiddleware``; each connection joins the
    per-profile group produced by
    ``urbanlens.dashboard.models.notifications.signals.notification_group_name``,
    which the ``NotificationLog`` post_save signal broadcasts to.

    A credential-authenticated connection additionally has to hold
    ``notifications:read``, the same scope the HTTP notification endpoints
    require - this socket is a live feed of the very same data, so letting any
    valid bearer token subscribe would make the scope meaningless. Session
    connections are unaffected (see :class:`CredentialScopeMixin`).

    The socket is strictly server → client: incoming frames are ignored, and
    marking notifications read stays on the existing HTMX endpoints. There is
    therefore no write scope to check.

    Close codes on ``connect()`` failure (mirroring ``SafetyCheckinChatConsumer``):

    - ``4404``: the session is unauthenticated, or the credential doesn't grant
      ``notifications:read`` - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.
    """

    async def connect(self):
        """Authenticate the session or credential, check scope, and join the profile's notification group."""
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4404)
            return

        # Checked before any group is joined, so a scope-refused connection never
        # becomes a member that a broadcast could reach even briefly.
        if not self.credential_allows(ApiKeyScope.NOTIFICATIONS_READ):
            logger.info("Notification socket rejected: credential lacks notifications:read (user %s)", getattr(user, "pk", None))
            await self.close(code=4404)
            return

        try:
            profile_id = await self._get_profile_id()
            from urbanlens.dashboard.models.notifications.signals import notification_group_name

            self.group_name = notification_group_name(profile_id)
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            self.start_credential_revalidation()
        except Exception:
            logger.exception("Notification socket connect failed for user %s", getattr(user, "pk", None))
            # Leave the group again if group_add succeeded but a later step (accept()) then
            # failed - Channels only reliably fires disconnect() for a connection that reached
            # accept(), so without this the group membership would otherwise leak.
            if hasattr(self, "group_name"):
                try:
                    await self.channel_layer.group_discard(self.group_name, self.channel_name)
                except Exception:
                    logger.exception("Notification socket failed to leave group %s during connect-failure cleanup", self.group_name)
            await self.close(code=4500)

    async def disconnect(self, close_code):
        """Leave the notification group, if we ever joined one, and stop re-validating the credential."""
        self.stop_credential_revalidation()
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Notification socket failed to leave group %s cleanly", self.group_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Ignore client frames - this socket is server → client only.

        Channels' base consumer calls ``receive(bytes_data=...)`` for a binary WS frame;
        accepting both keyword arguments (rather than only ``text_data``) keeps a stray binary
        frame from raising an uncaught ``TypeError`` that would otherwise kill the connection.
        """

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


class DirectMessageConsumer(CredentialScopeMixin, AsyncWebsocketConsumer):
    """Real-time direct-message channel for a logged-in user.

    Mounted at ``ws/messages/``. Authentication comes from the session cookie
    via Channels' ``AuthMiddlewareStack``, or from a ``?key=`` credential via
    ``ApiKeyAuthMiddleware`` - in which case ``messages:read`` is required to
    connect and ``messages:write`` to send. Those two scopes are in
    ``external_api.permissions.OAUTH2_ONLY_SCOPES``, so ``credential_grants``
    refuses them outright to a PAT-style ``ulk_`` key regardless of what its
    ``scopes`` list says: a long-lived bearer secret that tends to end up in CI
    configs and screenshots must not be a path into someone's DMs, and this
    socket is a live feed of exactly that. Before the check existed, a PAT could
    open this socket and route straight around the boundary every HTTP messaging
    route enforces.

    Each connection joins the
    per-profile group from ``services.messaging.direct_messages.direct_message_group_name``;
    ``create_direct_message`` broadcasts every new message to both the sender's
    and the recipient's groups, so all of either party's open tabs update at once.

    Sending: the client submits ``{"recipient": "<profile slug>", "body": "..."}``
    frames; validation, privacy enforcement, persistence, and the broadcast all
    live in ``create_direct_message`` - the same function the HTTP fallback
    (``ConversationSendView``) uses, mirroring the safety check-in chat split.

    Close codes on ``connect()`` failure (same contract as
    ``SafetyCheckinChatConsumer``, which the frontend reconnect logic branches on):

    - ``4404``: the session is unauthenticated, or the credential doesn't grant
      ``messages:read`` - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.
    """

    async def connect(self):
        """Authenticate the session or credential, check scope, join the profile's group, and mark them online."""
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4404)
            return

        # Checked before any group is joined, so a scope-refused connection never
        # becomes a member that an incoming message could be delivered to.
        if not self.credential_allows(ApiKeyScope.MESSAGES_READ):
            logger.info("Direct message socket rejected: credential lacks messages:read (user %s)", getattr(user, "pk", None))
            await self.close(code=4404)
            return

        try:
            self.profile_id = await self._get_profile_id()
            from urbanlens.dashboard.services.messaging.direct_messages import direct_message_group_name, mark_profile_online

            self.group_name = direct_message_group_name(self.profile_id)
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            self.start_credential_revalidation()
            await database_sync_to_async(mark_profile_online)(self.profile_id)
        except Exception:
            logger.exception("Direct message socket connect failed for user %s", getattr(user, "pk", None))
            # Leave the group again if group_add succeeded but a later step then failed -
            # Channels only reliably fires disconnect() for a connection that reached accept(),
            # so without this the group membership would otherwise leak.
            if hasattr(self, "group_name"):
                try:
                    await self.channel_layer.group_discard(self.group_name, self.channel_name)
                except Exception:
                    logger.exception("Direct message socket failed to leave group %s during connect-failure cleanup", self.group_name)
            await self.close(code=4500)

    async def disconnect(self, close_code):
        """Leave the direct-message group, mark one fewer live connection, and stop re-validating the credential."""
        self.stop_credential_revalidation()
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Direct message socket failed to leave group %s cleanly", self.group_name)
        if hasattr(self, "profile_id"):
            from urbanlens.dashboard.services.messaging.direct_messages import mark_profile_offline

            try:
                await database_sync_to_async(mark_profile_offline)(self.profile_id)
            except Exception:
                logger.exception("Direct message socket failed to mark profile %s offline", self.profile_id)

    async def receive(self, text_data=None, bytes_data=None):
        """Persist an incoming message; the service broadcasts it to both parties.

        Args:
            text_data: JSON string with ``recipient`` (profile slug), ``body``,
                and optional ``image_ids``/``markup_map_uuid``/``reply_to``
                fields. Unparseable frames, or frames with no recipient and no
                body/attachment at all, are silently ignored; a frame that
                fails validation or privacy checks gets an explicit
                ``{"type": "error", ...}`` reply so the sender always learns
                their message didn't go through.
            bytes_data: Unused - this socket is JSON-text-only. Channels' base
                consumer calls ``receive(bytes_data=...)`` for a binary WS frame,
                so accepting (and ignoring) it here keeps a stray binary frame
                from raising an uncaught ``TypeError`` that would kill the connection.
        """
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        if text_data is None:
            return
        # Every frame this socket accepts mutates something on the sender's
        # behalf - sending a message, broadcasting a typing indicator, marking a
        # thread read - so messages:write gates the whole method rather than only
        # the message branch. A messages:read credential is a listen-only grant.
        if not self.credential_allows(ApiKeyScope.MESSAGES_WRITE):
            await self.send(text_data=json.dumps({"type": "error", "detail": _INSUFFICIENT_SCOPE_DETAIL}))
            return
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Direct message socket received an unparseable frame from profile %s", self.profile_id)
            return

        if data.get("type") == "typing":
            recipient_slug = str(data.get("recipient") or "").strip()
            if recipient_slug:
                from urbanlens.dashboard.services.messaging.direct_messages import broadcast_typing_indicator

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
            # message fans out to every active member (see services.messaging.group_chats).
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
        from urbanlens.dashboard.services.messaging.group_chats import mark_group_thread_open

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
        from urbanlens.dashboard.services.messaging.group_chats import create_group_message

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
        from urbanlens.dashboard.services.messaging.direct_messages import mark_thread_open

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
        from urbanlens.dashboard.services.messaging.direct_messages import create_direct_message

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


class SafetyCheckinChatConsumer(CredentialScopeMixin, AsyncWebsocketConsumer):
    """Real-time chat for a safety check-in, shared by the owner, every accepted partner, and every emergency contact.

    Mounted under two routes (see ``dashboard/routing.py``):

    - ``ws/safety/checkin/<uuid:checkin_uuid>/chat/`` - session route, requires
      an authenticated session belonging to the check-in's owner or an accepted
      ``SafetyCheckinPartner`` (populated by Channels' ``AuthMiddlewareStack``
      from the session cookie; see ``services.visits.safety.is_owner_or_accepted_partner``),
      or the same identity established by a ``?key=`` credential through
      ``ApiKeyAuthMiddleware``. A credential additionally needs ``safety:read``
      to join and ``safety:write`` to send - being the check-in's owner is not
      by itself enough, exactly as it isn't on the HTTP safety endpoints. A
      ``pins:read``-only key must not be able to read someone's check-in chat
      just because it happens to belong to that someone.
    - ``ws/safety/contact/<uuid:token>/chat/`` - contact route, authorized by
      the token alone (mirrors ``SafetyCheckinMessageView._resolve`` - a
      contact identified only by email has no account to log into). No scope
      check applies here, ever: authority on this route comes from the
      magic-link token, not from a credential, and a contact opening their
      portal has no credential to check. A ``?key=`` that happens to ride along
      on this route is irrelevant to whether the connection is allowed.

    Everyone connected for a given check-in - the owner, its accepted
    partners, and all of its contacts - joins the same channel group, so a
    message from any one of them is broadcast to all the others immediately.
    The session route additionally joins a second, narrower group carrying
    live-location updates (``services.visits.safety._broadcast_live_location``) -
    contacts never join it, matching the model-level rule that live location
    is visible to partners only, never to emergency contacts.

    Close codes used on ``connect()`` failure (the frontend branches on these
    to decide whether to keep retrying):

    - ``4404``: the check-in/contact/token doesn't resolve, the owner
      route was hit while unauthenticated, or the credential doesn't grant
      ``safety:read`` - permanent, retrying won't help.
    - ``4500``: an unexpected server-side error - transient, safe to retry.

    Incoming frames that fail to save are answered with
    ``{"type": "error", "detail": "..."}`` rather than silently dropped or
    left to crash the socket, so a sender always learns their message didn't
    go through - important for a feature people may rely on in an emergency.
    """

    async def connect(self):
        """Resolve the check-in (and, on the contact route, the authorizing contact), check scope, then join its group."""
        from django.core.exceptions import ObjectDoesNotExist

        from urbanlens.dashboard.models.account.model import ApiKeyScope

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

        # Scope only ever restricts the session route. On the contact route the
        # token *is* the authorization (self.contact is set), and the connecting
        # party is typically an emergency contact with no account at all - making
        # them prove a scope would break the portal for the exact people it exists
        # to reach. Ordered after _resolve() because that is what tells the two
        # routes apart.
        if self.contact is None and not self.credential_allows(ApiKeyScope.SAFETY_READ):
            logger.info("Safety chat connection rejected: credential lacks safety:read (checkin %s)", self.checkin.pk)
            await self.close(code=4404)
            return

        from urbanlens.dashboard.services.visits.safety import safety_checkin_group_name, safety_checkin_location_group_name

        self.group_name = safety_checkin_group_name(self.checkin.pk)
        # Live location is never shared with token-route contacts (see the model's
        # own docstring on live_location_sharing_enabled) - only the session route
        # (owner or an accepted partner, both already verified by _resolve()) joins
        # the location group.
        self.location_group_name = safety_checkin_location_group_name(self.checkin.pk) if self.contact is None else None
        joined_groups = []
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            joined_groups.append(self.group_name)
            if self.location_group_name:
                await self.channel_layer.group_add(self.location_group_name, self.channel_name)
                joined_groups.append(self.location_group_name)
            await self.accept()
        except Exception:
            logger.exception("Safety chat failed to join group for checkin %s", self.checkin.pk)
            # Leave any group(s) already joined before accept() failed - Channels only
            # reliably fires disconnect() for a connection that reached accept(), so without
            # this the group membership would otherwise leak.
            for name in joined_groups:
                try:
                    await self.channel_layer.group_discard(name, self.channel_name)
                except Exception:
                    logger.exception("Safety chat failed to leave group %s during connect-failure cleanup", name)
            await self.close(code=4500)
            return
        logger.info("Safety chat connected: checkin=%s contact=%s", self.checkin.pk, getattr(self.contact, "pk", None))
        if self.contact is None:
            # Defense-in-depth against a dropped partner_access_revoked broadcast: that
            # group_send is best-effort, same as every other broadcast in this module, but
            # unlike the others it's the *only* mechanism that revokes an already-open
            # connection's access (permission is otherwise checked once, at connect() time).
            # A channel-layer hiccup at the exact moment a partner is removed would
            # otherwise leave their live-location stream open indefinitely with no
            # self-healing path. The token/contact route never joins the location group and
            # its access can't be revoked mid-connection (a magic link is either valid or
            # it isn't), so it doesn't need this.
            #
            # This same loop also carries the credential re-check for a ?key=
            # connection (_is_still_authorized consults _credential_is_still_valid),
            # which is why this consumer never calls
            # CredentialScopeMixin.start_credential_revalidation - doing both would
            # run two timers against the same connection for the same purpose. Only
            # the session route can carry a credential that matters here anyway: the
            # contact route is authorized by its token, not by a credential.
            self._revalidation_task = asyncio.create_task(self._revalidate_access_periodically())

    async def disconnect(self, close_code):
        """Leave the check-in's group(s), if we ever joined any, and stop re-validating."""
        task = getattr(self, "_revalidation_task", None)
        if task is not None:
            task.cancel()
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("Safety chat failed to leave group %s cleanly", self.group_name)
        if getattr(self, "location_group_name", None):
            try:
                await self.channel_layer.group_discard(self.location_group_name, self.channel_name)
            except Exception:
                logger.exception("Safety chat failed to leave group %s cleanly", self.location_group_name)

    async def _revalidate_access_periodically(self):
        """Periodically re-check that this session-route connection is still authorized.

        Closes the connection with code 4404 the moment it isn't - a backstop for when
        ``partner_access_revoked`` (services.visits.safety._broadcast_partner_access_revoked)
        never arrives.
        """
        try:
            while True:
                await asyncio.sleep(_PARTNER_REVALIDATION_INTERVAL_SECONDS)
                if not await self._is_still_authorized():
                    await self.close(code=4404)
                    return
        except asyncio.CancelledError:
            pass

    @database_sync_to_async
    def _is_still_authorized(self):
        """Re-check owner-or-accepted-partner status - and credential validity - fresh from the DB.

        Returns:
            True if this connection may continue: its credential (when it has
            one) is still unrevoked and unexpired, *and* its profile is still
            the owner or an accepted partner on this checkin. False if the
            checkin is gone, partner access was revoked, or the credential was
            revoked since connect() (or since the last periodic check).

            The credential arm matters on its own: a WebSocket authenticates
            once, at connect(), so revoking a leaked key would otherwise block
            only the next HTTP call while its already-open socket kept
            streaming this check-in's chat - and, for a partner, its live
            location - indefinitely.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.safety.model import SafetyCheckin
        from urbanlens.dashboard.services.visits.safety import is_owner_or_accepted_partner

        if not _credential_is_still_valid(self.credential):
            return False
        checkin = SafetyCheckin.objects.filter(pk=self.checkin.pk).first()
        if checkin is None:
            return False
        profile = Profile.objects.filter(pk=self.profile_id).first()
        if profile is None:
            return False
        return is_owner_or_accepted_partner(checkin, profile)

    async def receive(self, text_data=None, bytes_data=None):
        """Persist an incoming chat message and broadcast it to the check-in's group.

        Args:
            text_data: JSON string with a ``body`` field. Unparseable or
                blank frames are silently ignored - there's no message to
                report failure for. A frame that fails validation or fails
                to save gets an explicit ``{"type": "error", ...}`` reply
                instead.
            bytes_data: Unused - this socket is JSON-text-only. Accepting (and
                ignoring) it keeps a stray binary frame from raising an uncaught
                ``TypeError`` that would kill the connection.
        """
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        if text_data is None:
            return
        # safety:read is a listen-only grant on the session route. The contact
        # route is exempt for the same reason connect() exempts it: its authority
        # is the magic-link token, not a credential, and an emergency contact
        # opening their portal in a browser that happens to hold an unrelated
        # ``?key=`` must not be silenced by that key's scopes. _create_message()
        # below still applies every content and archival rule to whatever they send.
        if self.contact is None and not self.credential_allows(ApiKeyScope.SAFETY_WRITE):
            await self.send(text_data=json.dumps({"type": "error", "detail": _INSUFFICIENT_SCOPE_DETAIL}))
            return
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
                ``services.visits.safety._broadcast_status_update``).
        """
        await self.send(text_data=json.dumps(event["payload"]))

    async def location_update(self, event):
        """Deliver a live-location update to this connection.

        Only ever received on the session route's location group - see
        ``connect()``'s ``location_group_name`` gate.

        Args:
            event: The group-send event, with a ``payload`` dict (see
                ``services.visits.safety._broadcast_live_location``).
        """
        await self.send(text_data=json.dumps(event["payload"]))

    async def archive_scheduled(self, event):
        """Deliver a check-in's archival countdown to this connection.

        Delivered on the main ``safety_checkin_{pk}`` group - shared by the
        owner, its accepted partners, and its contacts, all of whom "were able
        to see" the check-in and so all get the same countdown.

        Args:
            event: The group-send event, with a ``payload`` dict (see
                ``services.visits.safety._broadcast_archive_scheduled``).
        """
        await self.send(text_data=json.dumps(event["payload"]))

    async def checkin_archived(self, event):
        """Deliver the "this check-in is now archived" event to this connection.

        Args:
            event: The group-send event, with a ``payload`` dict (see
                ``services.visits.safety._broadcast_checkin_archived``).
        """
        await self.send(text_data=json.dumps(event["payload"]))

    async def partner_access_revoked(self, event):
        """Force-close this connection if it belongs to the just-removed partner.

        Delivered to every connection on the check-in's group (owner, every partner,
        every contact) - only the one whose bound profile matches acts on it. This is
        the only way to revoke an already-open socket's access: permission is
        otherwise checked once, at ``connect()`` time, in ``_resolve()``.

        Args:
            event: The group-send event, with a ``payload`` dict containing the
                removed partner's ``profile_id`` (see
                ``services.visits.safety._broadcast_partner_access_revoked``).
        """
        if self.contact is not None or getattr(self, "profile_id", None) != event["payload"]["profile_id"]:
            return
        # No message frame first - the chat panel's onmessage switch has no case for
        # this and would otherwise fall through to appendMessage(). Closing with 4404
        # alone already makes the client show "You don't have access to this chat."
        # (see _chat_panel.html's onclose handler), same as a revoked contact token.
        await self.close(code=4404)

    @database_sync_to_async
    def _resolve(self, checkin_uuid, token):
        """Resolve the check-in and, for the contact route, the authorizing contact.

        Args:
            checkin_uuid: UUID of the check-in (session route).
            token: Contact's magic-link token (contact route).

        Returns:
            (checkin, contact) - contact is None on the session route.

        Raises:
            ObjectDoesNotExist: If the check-in/contact/token doesn't resolve, or the
                session route is used by someone who is neither the owner nor an
                accepted partner.
            PermissionError: If the session route is used while unauthenticated.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
        from urbanlens.dashboard.services.visits.safety import is_owner_or_accepted_partner

        if token is not None:
            self.profile_id = None
            contact = SafetyCheckinContact.objects.select_related("checkin").get(token=token)
            return contact.checkin, contact

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            raise PermissionError
        profile, _ = Profile.objects.get_or_create(user=user)
        checkin = SafetyCheckin.objects.get(uuid=checkin_uuid)
        if not is_owner_or_accepted_partner(checkin, profile):
            raise SafetyCheckin.DoesNotExist
        # Bound for the lifetime of this connection - used to re-check permission on
        # every incoming message (a partner's access can be removed after connect())
        # and to target this connection for partner_access_revoked().
        self.profile_id = profile.pk
        return checkin, None

    @database_sync_to_async
    def _create_message(self, body):
        """Create the chat message and serialize it for broadcast.

        Args:
            body: Message text.

        Returns:
            A JSON-serializable dict describing the new message.

        Raises:
            ValueError: Passed through from ``create_chat_message`` (blank/too-long
                body, or an archived check-in), or raised here directly if the
                session route's profile is no longer the owner or an accepted
                partner - permission was only checked once, at ``connect()`` time,
                so a partner removed mid-connection must be re-checked on every send.
        """
        from django.contrib.auth.models import AnonymousUser

        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.visits.safety import create_chat_message, is_owner_or_accepted_partner

        user = self.scope.get("user") or AnonymousUser()
        if self.contact is None:
            profile, _ = Profile.objects.get_or_create(user=user)
            if not is_owner_or_accepted_partner(self.checkin, profile):
                raise ValueError("You no longer have access to this check-in.")
        message = create_chat_message(self.checkin, user=user, contact=self.contact, body=body)
        return {
            "type": "message",
            "id": message.pk,
            "sender_name": message.sender_name,
            "body": message.body,
            "created": message.created.isoformat(),
        }


class _ParticipantSessionConsumer(CredentialScopeMixin, AsyncWebsocketConsumer):
    """Shared real-time sync for one participant-based game session.

    Both ``GameSessionConsumer`` (SpotGuessr) and ``TriviaSessionConsumer``
    (Trivia) are one channel-layer group per session, every participant
    (JOINED or still just INVITED) joining the same group, an inbound socket
    that only ever accepts a chat message frame, and every state-changing
    action (join, start, guess/answer) staying a durable HTTP POST that
    broadcasts through this socket rather than originating from it. This base
    holds everything that isn't actually game-specific - a subclass supplies
    only: ``game_label`` (for log messages), ``_group_name()``,
    ``_is_participant()``, and ``_send_chat_message()``. (Before this base
    existed, ``TriviaSessionConsumer`` was a hand-copied "exact mirror" of
    ``GameSessionConsumer`` - every docstring sentence duplicated along with
    the code, which is exactly how the two would eventually drift.)

    This differs deliberately from ``DirectMessageConsumer``'s per-profile-group
    shape: every participant needs the identical broadcast here, unlike a
    DM/group-chat's per-viewer identity-masked payloads, which don't apply to
    a game session (participants already see each other by name on the
    scoreboard).

    A credential-authenticated connection additionally has to hold
    ``games:read`` to connect and ``games:write`` to send a chat frame - the
    same scopes the HTTP game endpoints require, because this socket carries
    the identical data (live round payloads, reveals, the scoreboard) and
    accepts a write on the user's behalf. Before that check existed, a
    credential granting nothing but ``pins:read`` could open
    ``ws/spotguessr/session/<id>/`` (or the Trivia/Consensus equivalents) and
    both read and post, routing straight around the boundary every HTTP route
    enforces. Session connections are unaffected (see
    :class:`CredentialScopeMixin`), so the web client's behavior is unchanged.

    Close codes on ``connect()`` failure (same convention as every other
    consumer here): ``4404`` permanent (unauthenticated, not a participant of
    this session, or a credential without ``games:read`` - never
    distinguished, so a session someone isn't part of doesn't even reveal that
    it exists), ``4500`` transient/retryable.
    """

    #: Overridden per subclass, purely for log messages (e.g. "SpotGuessr", "Trivia").
    game_label = "Game"

    def _group_name(self, session_id) -> str:
        """The channel-layer group this session's participants share - see the game's own ``realtime`` module."""
        raise NotImplementedError

    async def _is_participant(self, session_id, user) -> bool:
        """Whether ``user``'s profile is a participant (any status) of ``session_id``."""
        raise NotImplementedError

    async def _send_chat_message(self, body: str) -> None:
        """Save and broadcast one chat message from this connection's profile."""
        raise NotImplementedError

    async def connect(self):
        """Verify the connecting profile is a scoped participant (any status) of this session, then join its group."""
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        session_id = self.scope["url_route"]["kwargs"].get("session_id")
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4404)
            return

        # Checked before the participant lookup and before any group is joined,
        # so a scope-refused connection never becomes a member a broadcast could
        # reach even briefly - and never gets to distinguish a real session from
        # an invented one by how long the refusal takes.
        if not self.credential_allows(ApiKeyScope.GAMES_READ):
            logger.info("%s socket rejected: credential lacks games:read (user %s)", self.game_label, getattr(user, "pk", None))
            await self.close(code=4404)
            return

        try:
            is_participant = await self._is_participant(session_id, user)
        except Exception:
            logger.exception("%s socket connect failed unexpectedly for session %s", self.game_label, session_id)
            await self.close(code=4500)
            return

        if not is_participant:
            await self.close(code=4404)
            return

        self.session_id = session_id
        self.group_name = self._group_name(session_id)
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            self.start_credential_revalidation()
        except Exception:
            logger.exception("%s socket failed to join group for session %s", self.game_label, session_id)
            # Leave the group again if group_add succeeded but accept() then failed -
            # Channels only reliably fires disconnect() for a connection that reached
            # accept(), so without this the group membership would otherwise leak.
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("%s socket failed to leave group %s during connect-failure cleanup", self.game_label, self.group_name)
            await self.close(code=4500)

    async def disconnect(self, close_code):
        """Leave the session's group, if we ever joined one, and stop re-validating the credential."""
        self.stop_credential_revalidation()
        if hasattr(self, "group_name"):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.exception("%s socket failed to leave group %s cleanly", self.game_label, self.group_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Persist an incoming chat message and broadcast it - the only client-to-server frame this socket accepts.

        Args:
            text_data: JSON string with a ``body`` field. Unparseable or
                blank frames are silently ignored - there's no message to
                report failure for.
            bytes_data: Unused - this socket is JSON-text-only. Accepting (and
                ignoring) it keeps a stray binary frame from raising an uncaught
                ``TypeError`` that would kill the connection.
        """
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        if text_data is None:
            return
        # Every frame this socket accepts writes something on the sender's
        # behalf, so games:write gates the whole method: a games:read credential
        # is a listen-only grant. Reported as an error frame rather than a close,
        # matching DirectMessageConsumer - closing would put the client into a
        # reconnect loop over a condition retrying cannot fix.
        if not self.credential_allows(ApiKeyScope.GAMES_WRITE):
            await self.send(text_data=json.dumps({"type": "error", "detail": _INSUFFICIENT_SCOPE_DETAIL}))
            return
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("%s socket received an unparseable frame on session %s", self.game_label, self.session_id)
            return
        body = str(data.get("body") or "").strip()
        if not body:
            return

        try:
            await self._send_chat_message(body)
        except Exception:
            logger.exception("%s chat message failed on session %s", self.game_label, self.session_id)
            await self.send(text_data=json.dumps({"type": "error", "detail": "Your message couldn't be sent. Please try again."}))

    async def _relay(self, event):
        """Forward a broadcasted event straight to this connection, verbatim."""
        await self.send(text_data=json.dumps(event))

    async def participant_joined(self, event):
        await self._relay(event)

    #: Fired when a participant voluntarily leaves or is kicked by the host
    #: (``event["reason"]`` distinguishes the two) - currently Trivia-only
    #: (``services.trivia.session.leave_session``/``kick_participant``),
    #: but lives on the shared base since it's generic relay logic, not
    #: game-specific.
    async def participant_left(self, event):
        await self._relay(event)

    async def session_started(self, event):
        await self._relay(event)

    #: SpotGuessr's own round-completing action.
    async def guess_submitted(self, event):
        await self._relay(event)

    #: Trivia's own round-completing action - also reused verbatim by
    #: Consensus for its own answer-submission event (both games broadcast
    #: the same "someone answered" shape, and each session is already
    #: scoped to its own channel-layer group).
    async def answer_submitted(self, event):
        await self._relay(event)

    async def round_revealed(self, event):
        await self._relay(event)

    #: Consensus-only: a participant cast their vote during a competitive
    #: round's disagreement sub-phase - the vote-open state itself (and its
    #: candidate values) rides on the same ``round_revealed`` broadcast
    #: every round resolution already sends (see
    #: ``services.consensus.serializers.serialize_round_reveal``'s
    #: ``vote_options``), so no separate "vote opened" event is needed.
    async def vote_submitted(self, event):
        await self._relay(event)

    async def round_started(self, event):
        await self._relay(event)

    async def session_completed(self, event):
        await self._relay(event)

    async def chat_message(self, event):
        await self._relay(event)


class GameSessionConsumer(_ParticipantSessionConsumer):
    """Real-time sync for one SpotGuessr session, shared by every participant (UL-392).

    Mounted at ``ws/spotguessr/session/<int:session_id>/``. See
    "Real-time sync: GameSessionConsumer" in ``docs/designs/spotguessr.md``
    for the full event catalogue; ``_ParticipantSessionConsumer`` documents
    everything about this socket that isn't SpotGuessr-specific.
    """

    game_label = "SpotGuessr"

    def _group_name(self, session_id) -> str:
        from urbanlens.dashboard.services.spotguessr.realtime import session_group_name

        return session_group_name(session_id)

    @database_sync_to_async
    def _is_participant(self, session_id, user):
        """Whether ``user``'s profile is a participant (any status) of ``session_id``.

        Returns:
            False for a nonexistent session or a real session the profile
            just isn't part of - deliberately not distinguished, matching
            the 404-not-403 convention used everywhere else in this feature.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.spotguessr.model import GameSessionParticipant

        profile, _ = Profile.objects.get_or_create(user=user)
        return GameSessionParticipant.objects.filter(session_id=session_id, profile=profile).exists()

    @database_sync_to_async
    def _send_chat_message(self, body):
        """Save and broadcast one chat message from this connection's profile.

        Broadcasting happens inside ``services.spotguessr.chat.send_chat_message``
        itself (not here) since the save-then-broadcast choke point is
        shared with any future non-WebSocket sender.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.spotguessr.model import GameSession
        from urbanlens.dashboard.services.spotguessr.chat import send_chat_message

        profile = Profile.objects.get(user=self.scope["user"])
        session = GameSession.objects.get(pk=self.session_id)
        send_chat_message(session, profile, body)


class TriviaSessionConsumer(_ParticipantSessionConsumer):
    """Real-time sync for one Trivia session, shared by every participant.

    Mounted at ``ws/trivia/session/<int:session_id>/``. See
    ``_ParticipantSessionConsumer`` for everything about this socket that
    isn't Trivia-specific.
    """

    game_label = "Trivia"

    def _group_name(self, session_id) -> str:
        from urbanlens.dashboard.services.trivia.realtime import session_group_name

        return session_group_name(session_id)

    @database_sync_to_async
    def _is_participant(self, session_id, user):
        """Whether ``user``'s profile is a participant (any status) of ``session_id``.

        Returns:
            False for a nonexistent session or a real session the profile
            just isn't part of - deliberately not distinguished, matching
            the 404-not-403 convention used everywhere else in this feature.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.trivia.model import TriviaSessionParticipant

        profile, _ = Profile.objects.get_or_create(user=user)
        return TriviaSessionParticipant.objects.filter(session_id=session_id, profile=profile).exists()

    @database_sync_to_async
    def _send_chat_message(self, body):
        """Save and broadcast one chat message from this connection's profile.

        Broadcasting happens inside ``services.trivia.chat.send_chat_message``
        itself (not here) since the save-then-broadcast choke point is
        shared with any future non-WebSocket sender.
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.trivia.model import TriviaSession
        from urbanlens.dashboard.services.trivia.chat import send_chat_message

        profile = Profile.objects.get(user=self.scope["user"])
        session = TriviaSession.objects.get(pk=self.session_id)
        send_chat_message(session, profile, body)


class ConsensusSessionConsumer(_ParticipantSessionConsumer):
    """Real-time sync for one competitive Consensus session, shared by every participant.

    Mounted at ``ws/consensus/session/<int:session_id>/``. See
    ``_ParticipantSessionConsumer`` for everything about this socket that
    isn't Consensus-specific. Solo sessions never open a socket at all.
    """

    game_label = "Consensus"

    def _group_name(self, session_id) -> str:
        from urbanlens.dashboard.services.consensus.realtime import session_group_name

        return session_group_name(session_id)

    @database_sync_to_async
    def _is_participant(self, session_id, user):
        """Whether ``user``'s profile is a participant (any status) of ``session_id``.

        Returns:
            False for a nonexistent session or a real session the profile
            just isn't part of - deliberately not distinguished, matching
            the 404-not-403 convention used everywhere else in this feature.
        """
        from urbanlens.dashboard.models.consensus.model import ConsensusSessionParticipant
        from urbanlens.dashboard.models.profile.model import Profile

        profile, _ = Profile.objects.get_or_create(user=user)
        return ConsensusSessionParticipant.objects.filter(session_id=session_id, profile=profile).exists()

    @database_sync_to_async
    def _send_chat_message(self, body):
        """Save and broadcast one chat message from this connection's profile.

        Broadcasting happens inside ``services.consensus.chat.send_chat_message``
        itself (not here) since the save-then-broadcast choke point is
        shared with any future non-WebSocket sender.
        """
        from urbanlens.dashboard.models.consensus.model import ConsensusSession
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.consensus.chat import send_chat_message

        profile = Profile.objects.get(user=self.scope["user"])
        session = ConsensusSession.objects.get(pk=self.session_id)
        send_chat_message(session, profile, body)
