"""Scope enforcement on the game-session WebSockets (SpotGuessr / Trivia / Consensus).

``_ParticipantSessionConsumer`` originally gated only on ``user.is_authenticated``
plus participant membership. Once ``ApiKeyAuthMiddleware`` let an external
credential open a socket, that meant a credential holding nothing but
``pins:read`` could open ``ws/spotguessr/session/<id>/`` and both read the live
round/reveal broadcasts and post into the session chat - routing straight around
the ``games:read``/``games:write`` boundary every HTTP game endpoint enforces.

These tests pin the fix down from both directions: a credential without the
scope must be refused, and a *session* connection (the web client, which has no
credential at all) must be completely unaffected - the whole point of
``CredentialScopeMixin`` short-circuiting on ``credential is None``.

Uses TransactionTestCase for the same reason ``test_spotguessr_consumer`` does:
Channels consumers reach the database from a background thread via
``database_sync_to_async``.
"""

from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.consumers import GameSessionConsumer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import SpotGuessrMode
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.spotguessr.session import GameConfig, start_solo_session
from urbanlens.dashboard.websocket_auth import CREDENTIAL_SCOPE_KEY

_IN_MEMORY_CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _run(coro):
    """Run *coro* via async_to_sync so database_sync_to_async's thread bridge is actually pumped."""

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _make_profile() -> Profile:
    """A fresh profile with its auto-created user."""
    return Profile.objects.get(user=baker.make("auth.User"))


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class GameSessionSocketScopeTests(TransactionTestCase):
    """A credential opening a game socket has to hold the games scopes."""

    def setUp(self) -> None:
        """Create a solo session whose host is the profile every test connects as."""
        self.host = _make_profile()
        # Force the user relation now, synchronously: the async test bodies must
        # never trigger a lazy ORM fetch, which Django's async-safety guard blocks.
        _ = self.host.user
        self.session = start_solo_session(self.host, SpotGuessrMode.PHOTOS, GameConfig())
        # Every credential is issued here, synchronously, for the same reason
        # ``self.host.user`` is forced above: the async test bodies run on an
        # event loop with no thread bridge, so any ORM call from inside one
        # raises SynchronousOnlyOperation rather than exercising the consumer.
        self.pins_only_key = self._key([ApiKeyScope.PINS_READ])
        self.games_read_key = self._key([ApiKeyScope.GAMES_READ])
        self.games_write_key = self._key([ApiKeyScope.GAMES_READ, ApiKeyScope.GAMES_WRITE])

    def _key(self, scopes: list[ApiKeyScope]) -> ApiKey:
        """Issue an API key for the host carrying exactly *scopes*."""
        api_key, _raw = generate_api_key(self.host.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
        return ApiKey.objects.get(pk=api_key.pk)

    def _communicator(self, credential: ApiKey | None = None) -> WebsocketCommunicator:
        """A communicator for the host's session, optionally carrying a credential."""
        comm = WebsocketCommunicator(GameSessionConsumer.as_asgi(), f"/ws/spotguessr/session/{self.session.pk}/")
        comm.scope["url_route"] = {"kwargs": {"session_id": self.session.pk}}
        comm.scope["user"] = self.host.user
        comm.scope[CREDENTIAL_SCOPE_KEY] = credential
        return comm

    def test_a_credential_without_games_read_is_refused(self) -> None:
        _run(self._a_credential_without_games_read_is_refused())

    async def _a_credential_without_games_read_is_refused(self) -> None:
        comm = self._communicator(self.pins_only_key)
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_a_credential_with_games_read_may_connect(self) -> None:
        _run(self._a_credential_with_games_read_may_connect())

    async def _a_credential_with_games_read_may_connect(self) -> None:
        comm = self._communicator(self.games_read_key)
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.disconnect()

    def test_a_read_only_credential_cannot_post_chat(self) -> None:
        _run(self._a_read_only_credential_cannot_post_chat())

    async def _a_read_only_credential_cannot_post_chat(self) -> None:
        comm = self._communicator(self.games_read_key)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "let me in"}))
        reply = json.loads(await comm.receive_from())
        self.assertEqual(reply["type"], "error")
        # A frame-level error, not a close: retrying cannot fix a missing scope,
        # so closing would only put the client into a reconnect loop.
        self.assertTrue(await comm.receive_nothing(timeout=0.2))

        await comm.disconnect()

    def test_a_credential_with_games_write_may_post_chat(self) -> None:
        _run(self._a_credential_with_games_write_may_post_chat())

    async def _a_credential_with_games_write_may_post_chat(self) -> None:
        comm = self._communicator(self.games_write_key)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "hello"}))
        reply = json.loads(await comm.receive_from())
        self.assertEqual(reply["type"], "chat.message")
        self.assertEqual(reply["message"]["body"], "hello")

        await comm.disconnect()

    def test_a_session_connection_is_unaffected(self) -> None:
        _run(self._a_session_connection_is_unaffected())

    async def _a_session_connection_is_unaffected(self) -> None:
        """No credential in the scope means the web client, which these rules must not touch."""
        comm = self._communicator(None)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "web client"}))
        reply = json.loads(await comm.receive_from())
        self.assertEqual(reply["type"], "chat.message")

        await comm.disconnect()
