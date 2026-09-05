"""Scope enforcement for credential-authenticated WebSocket connections.

``ApiKeyAuthMiddleware`` lets a PAT-style ``ApiKey`` or an OAuth2 access token
authenticate a Channels socket the same way it authenticates an HTTP request to
``external_api``. HTTP additionally runs
``external_api.permissions.HasApiKeyScope`` on every view, so a credential can
only reach the domains its grant names; the sockets originally ran no such
check, which meant one bearer credential unlocked *every* socket regardless of
its scopes. These tests pin the three holes that opened up:

- a ``pins:read``-only key could join someone's safety check-in chat;
- a PAT could open ``ws/messages/`` and receive live direct messages, even
  though ``messages:read``/``messages:write`` are in ``OAUTH2_ONLY_SCOPES``
  and are refused for PAT-kind credentials on every HTTP route;
- revoking a key left its already-open socket delivering indefinitely.

They also pin the two things that must *not* change: a browser-session
connection carries no credential and is unaffected, and the tokenized safety
contact portal is authorized by its magic-link token rather than by a
credential, so a stray ``?key=`` on that route changes nothing.

Uses ``TransactionTestCase`` (not the project's default ``TestCase``) for the
same reason ``test_safety_chat.py`` does: consumers reach the database from a
background thread via ``database_sync_to_async``, and the revocation tests rely
on a committed write becoming visible to that thread.
"""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch
import uuid

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.dashboard.consumers import DirectMessageConsumer, SafetyCheckinChatConsumer, UserNotificationConsumer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

Application = get_application_model()
AccessToken = get_access_token_model()


def _run(coro):
    """Drive *coro* through ``async_to_sync`` rather than ``asyncio.run``.

    ``database_sync_to_async``'s thread-sensitive mode needs the
    ``CurrentThreadExecutor`` that only ``async_to_sync``'s sync->async->sync
    bridge installs; under a bare ``asyncio.run`` nothing pumps that queue and
    the first consumer DB access hangs forever instead of completing.

    Args:
        coro: The coroutine to run to completion.

    Returns:
        Whatever *coro* returns.
    """

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _issue_key(user, *scopes: ApiKeyScope) -> str:
    """Issue a PAT-style key for *user* granting exactly *scopes*.

    ``ApiKey.scopes`` is ``editable=False`` and defaults to the fixed
    starter grant, so the scope list is rewritten with a queryset ``update``
    - the same way a future scope-picker UI would.

    Args:
        user: Owner of the new key.
        *scopes: The scope values the key should grant.

    Returns:
        The one-time plaintext key string, ready to put in a ``?key=`` param.
    """
    api_key, raw_key = generate_api_key(user, "Mobile app")
    ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
    return raw_key


def _issue_oauth2_token(user, scope: str, *, token: str) -> str:
    """Mint an OAuth2 access token for *user* carrying *scope*.

    Args:
        user: The resource owner the token acts for.
        scope: Space-separated scope string, as django-oauth-toolkit stores it.
        token: The literal token value (tests use readable constants).

    Returns:
        The token string.
    """
    application = Application.objects.create(
        name=f"UrbanLens Mobile {token}",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="urbanlens://oauth/callback",
    )
    AccessToken.objects.create(
        user=user,
        application=application,
        token=token,
        expires=timezone.now() + timedelta(hours=1),
        scope=scope,
    )
    return token


class SafetyChatCredentialScopeTests(TransactionTestCase):
    """``ws/safety/checkin/<uuid>/chat/`` requires safety:read to join and safety:write to send."""

    def setUp(self) -> None:
        """Create the bootstrap admin, a check-in owner, and one emergency contact."""
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.owner_user = baker.make("auth.User")
        self.owner_profile = self.owner_user.profile
        self.checkin = baker.make("dashboard.SafetyCheckin", profile=self.owner_profile)
        self.contact = baker.make(
            "dashboard.SafetyCheckinContact",
            checkin=self.checkin,
            contact_profile=None,
            email="contact@example.com",
        )

    def _owner_route(self, *, raw_key: str | None = None, user=None) -> WebsocketCommunicator:
        """Build a communicator for the owner/partner (session) route, optionally bearing a credential."""
        query = f"?key={raw_key}" if raw_key else ""
        comm = WebsocketCommunicator(
            ApiKeyAuthMiddleware(SafetyCheckinChatConsumer.as_asgi()),
            f"/ws/safety/checkin/{self.checkin.uuid}/chat/{query}",
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = user if user is not None else AnonymousUser()
        return comm

    def _contact_route(self, token, *, raw_key: str | None = None) -> WebsocketCommunicator:
        """Build a communicator for the tokenized emergency-contact portal route."""
        query = f"?key={raw_key}" if raw_key else ""
        comm = WebsocketCommunicator(
            ApiKeyAuthMiddleware(SafetyCheckinChatConsumer.as_asgi()),
            f"/ws/safety/contact/{token}/chat/{query}",
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": None, "token": str(token)}}
        comm.scope["user"] = AnonymousUser()
        return comm

    def test_key_without_safety_read_cannot_join_the_chat(self) -> None:
        """A pins-only key must not reach the owner's safety chat, even though it authenticates."""
        raw_key = _issue_key(self.owner_user, ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE)

        async def _test():
            comm = self._owner_route(raw_key=raw_key)
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_key_with_safety_read_can_join(self) -> None:
        """The correctly scoped key connects exactly as a session does."""
        raw_key = _issue_key(self.owner_user, ApiKeyScope.SAFETY_READ)

        async def _test():
            comm = self._owner_route(raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_read_only_key_cannot_send_messages(self) -> None:
        """safety:read is a listen-only grant - a send gets an error frame, not a saved message."""
        from urbanlens.dashboard.models.safety.model import SafetyCheckinMessage

        raw_key = _issue_key(self.owner_user, ApiKeyScope.SAFETY_READ)

        async def _test():
            comm = self._owner_route(raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_to(text_data=json.dumps({"body": "Sent by a read-only key"}))
            reply = json.loads(await comm.receive_from())
            self.assertEqual(reply["type"], "error")
            await comm.disconnect()

        _run(_test())
        self.assertFalse(SafetyCheckinMessage.objects.filter(body="Sent by a read-only key").exists())

    def test_key_with_safety_write_can_send(self) -> None:
        """safety:read + safety:write behaves like the session route end to end."""
        raw_key = _issue_key(self.owner_user, ApiKeyScope.SAFETY_READ, ApiKeyScope.SAFETY_WRITE)

        async def _test():
            comm = self._owner_route(raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_to(text_data=json.dumps({"body": "Heading home"}))
            echo = json.loads(await comm.receive_from())
            self.assertEqual(echo["body"], "Heading home")
            await comm.disconnect()

        _run(_test())

    def test_session_connection_is_unaffected(self) -> None:
        """No credential means no scope check - the web client keeps working unchanged."""

        async def _test():
            comm = self._owner_route(user=self.owner_user)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_to(text_data=json.dumps({"body": "From the browser"}))
            echo = json.loads(await comm.receive_from())
            self.assertEqual(echo["body"], "From the browser")
            await comm.disconnect()

        _run(_test())

    def test_contact_token_route_is_not_scope_checked(self) -> None:
        """The magic-link portal authorizes by token; an unrelated key riding along changes nothing."""
        outsider = baker.make("auth.User")
        raw_key = _issue_key(outsider, ApiKeyScope.PINS_READ)

        async def _test():
            comm = self._contact_route(self.contact.token, raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_to(text_data=json.dumps({"body": "Contact checking in"}))
            echo = json.loads(await comm.receive_from())
            self.assertEqual(echo["body"], "Contact checking in")
            await comm.disconnect()

        _run(_test())

    def test_contact_token_route_still_rejects_a_bad_token(self) -> None:
        """The token remains the authority on that route - a key cannot substitute for it."""
        raw_key = _issue_key(self.owner_user, ApiKeyScope.SAFETY_READ, ApiKeyScope.SAFETY_WRITE)

        async def _test():
            comm = self._contact_route(uuid.uuid4(), raw_key=raw_key)
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    @patch("urbanlens.dashboard.consumers._PARTNER_REVALIDATION_INTERVAL_SECONDS", 0.05)
    def test_revoking_the_key_closes_an_open_socket(self) -> None:
        """Revocation must terminate delivery, not merely block the next HTTP call."""
        api_key, raw_key = generate_api_key(self.owner_user, "Mobile app")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.SAFETY_READ.value])

        async def _test():
            comm = self._owner_route(raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)

            await database_sync_to_async(ApiKey.objects.filter(pk=api_key.pk).update)(revoked_at=timezone.now())

            message = await comm.receive_output(timeout=5)
            self.assertEqual(message["type"], "websocket.close")
            self.assertEqual(message["code"], 4404)

        _run(_test())


class DirectMessageCredentialScopeTests(TransactionTestCase):
    """``ws/messages/`` must honor the OAuth2-only boundary that guards the DM domain over HTTP."""

    def setUp(self) -> None:
        """Create the bootstrap admin plus the account whose DMs are at stake."""
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")

    def _communicator(self, *, raw_key: str | None = None, user=None) -> WebsocketCommunicator:
        """Build a ``ws/messages/`` communicator, optionally bearing a credential."""
        query = f"?key={raw_key}" if raw_key else ""
        comm = WebsocketCommunicator(ApiKeyAuthMiddleware(DirectMessageConsumer.as_asgi()), f"/ws/messages/{query}")
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = user if user is not None else AnonymousUser()
        return comm

    def test_pat_cannot_open_the_direct_message_socket(self) -> None:
        """A PAT is refused messages:* on every HTTP route; the socket must not route around it."""
        raw_key = _issue_key(self.user, ApiKeyScope.MESSAGES_READ, ApiKeyScope.MESSAGES_WRITE)

        async def _test():
            comm = self._communicator(raw_key=raw_key)
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_oauth2_token_with_messages_read_can_open_the_socket(self) -> None:
        """A user-consented OAuth2 token is the credential kind that *is* allowed here."""
        token = _issue_oauth2_token(self.user, "messages:read", token="tok-dm-read")

        async def _test():
            comm = self._communicator(raw_key=token)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_oauth2_token_without_messages_read_is_refused(self) -> None:
        """A token scoped to something else entirely cannot listen in on DMs."""
        token = _issue_oauth2_token(self.user, "pins:read", token="tok-dm-pins")

        async def _test():
            comm = self._communicator(raw_key=token)
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_read_only_oauth2_token_cannot_send(self) -> None:
        """messages:read is listen-only: an outbound frame is refused rather than persisted."""
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage
        from urbanlens.dashboard.models.profile.meta import VisibilityChoice

        recipient = baker.make("auth.User")
        # Open the recipient's DM privacy right up, so the *only* thing that can
        # refuse this send is the missing messages:write scope. Left at the
        # ANYTHING_IN_COMMON default, two unrelated accounts fail the privacy
        # check first and the test would pass without any scope check existing.
        recipient.profile.direct_message_visibility = VisibilityChoice.ANYONE
        recipient.profile.save(update_fields=["direct_message_visibility"])
        token = _issue_oauth2_token(self.user, "messages:read", token="tok-dm-readonly")

        async def _test():
            comm = self._communicator(raw_key=token)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_to(text_data=json.dumps({"recipient": recipient.profile.slug, "body": "Sent read-only"}))
            reply = json.loads(await comm.receive_from())
            self.assertEqual(reply["type"], "error")
            await comm.disconnect()

        _run(_test())
        self.assertFalse(DirectMessage.objects.filter(body="Sent read-only").exists())

    def test_session_connection_is_unaffected(self) -> None:
        """A logged-in browser tab still opens the DM socket with no scopes involved at all."""

        async def _test():
            comm = self._communicator(user=self.user)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    @patch("urbanlens.dashboard.consumers._CREDENTIAL_REVALIDATION_INTERVAL_SECONDS", 0.05)
    def test_revoking_the_token_closes_an_open_socket(self) -> None:
        """django-oauth-toolkit revokes by deleting the row; the live socket must notice."""
        token = _issue_oauth2_token(self.user, "messages:read", token="tok-dm-revoked")

        async def _test():
            comm = self._communicator(raw_key=token)
            connected, _ = await comm.connect()
            self.assertTrue(connected)

            await database_sync_to_async(AccessToken.objects.filter(token=token).delete)()

            message = await comm.receive_output(timeout=5)
            self.assertEqual(message["type"], "websocket.close")
            self.assertEqual(message["code"], 4404)

        _run(_test())


class NotificationCredentialScopeTests(TransactionTestCase):
    """``ws/notifications/`` requires notifications:read of a credential caller."""

    def setUp(self) -> None:
        """Create the bootstrap admin plus the notification recipient."""
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")

    def _communicator(self, *, raw_key: str | None = None, user=None) -> WebsocketCommunicator:
        """Build a ``ws/notifications/`` communicator, optionally bearing a credential."""
        query = f"?key={raw_key}" if raw_key else ""
        comm = WebsocketCommunicator(
            ApiKeyAuthMiddleware(UserNotificationConsumer.as_asgi()), f"/ws/notifications/{query}"
        )
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = user if user is not None else AnonymousUser()
        return comm

    def test_key_without_notifications_read_is_refused(self) -> None:
        """The default starter grant (pins/profile/push) does not include the notification firehose."""
        raw_key = _issue_key(self.user, ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE)

        async def _test():
            comm = self._communicator(raw_key=raw_key)
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_key_with_notifications_read_connects(self) -> None:
        """The correctly scoped key joins the profile's notification group as before."""
        raw_key = _issue_key(self.user, ApiKeyScope.NOTIFICATIONS_READ)

        async def _test():
            comm = self._communicator(raw_key=raw_key)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_session_connection_is_unaffected(self) -> None:
        """A logged-in browser tab needs no scopes to receive its own notifications."""

        async def _test():
            comm = self._communicator(user=self.user)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())
