"""Tests for GameSessionConsumer - real-time sync for multiplayer SpotGuessr sessions (UL-392).

Uses TransactionTestCase (not the project's default TestCase) because Channels
consumers touch the database from a background thread via
``database_sync_to_async`` - Channels' own testing docs call out
TransactionTestCase as the safe choice for exactly this reason. CHANNEL_LAYERS
is overridden to the in-memory backend so these tests don't need a real
Valkey/Redis connection. Mirrors test_safety_chat.py's established pattern.
"""

from __future__ import annotations

from itertools import count
import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.celery_inline import broadcasts_delivered_inline
from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.dashboard.consumers import GameSessionConsumer
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import SpotGuessrMode
from urbanlens.dashboard.models.subscriptions import UserSubscription
from urbanlens.dashboard.services.spotguessr.session import GameConfig, start_multiplayer_session

_coordinate_counter = count()


def _run(coro):
    """Run *coro* via async_to_sync so database_sync_to_async's thread bridge is actually pumped."""

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile(*, alpha: bool = True) -> Profile:
    user = baker.make("auth.User")
    if alpha:
        # The socket is gated on the same entitlement as every game HTTP route.
        grant_alpha_features(user)
    return Profile.objects.get(user=user)


class GameSessionConsumerTests(TransactionTestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        self.outsider = _make_profile()
        # Force these onto the instances now, in this synchronous setUp -
        # the async test bodies below must never trigger a *lazy* DB query
        # (e.g. a first-touch of `.user`), since Django's async-safety guard
        # blocks ORM access from a running event loop with no thread bridge.
        _ = self.host.user, self.guest.user, self.outsider.user

        friendship = Friendship.request(self.host, self.guest)
        assert friendship is not None
        friendship.accept()
        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])

    def _communicator(self, user) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(GameSessionConsumer.as_asgi(), f"/ws/spotguessr/session/{self.session.pk}/")
        comm.scope["url_route"] = {"kwargs": {"session_id": self.session.pk}}
        comm.scope["user"] = user
        return comm

    def test_a_participant_can_connect(self) -> None:
        _run(self._a_participant_can_connect())

    async def _a_participant_can_connect(self) -> None:
        comm = self._communicator(self.host.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.disconnect()

    def test_an_invited_but_not_yet_joined_participant_can_also_connect(self) -> None:
        _run(self._an_invited_but_not_yet_joined_participant_can_also_connect())

    async def _an_invited_but_not_yet_joined_participant_can_also_connect(self) -> None:
        comm = self._communicator(self.guest.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.disconnect()

    def test_a_non_participant_is_rejected(self) -> None:
        _run(self._a_non_participant_is_rejected())

    async def _a_non_participant_is_rejected(self) -> None:
        comm = self._communicator(self.outsider.user)
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_an_unauthenticated_connection_is_rejected(self) -> None:
        _run(self._an_unauthenticated_connection_is_rejected())

    async def _an_unauthenticated_connection_is_rejected(self) -> None:
        comm = self._communicator(AnonymousUser())
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_a_participant_without_alpha_features_is_rejected(self) -> None:
        """Gating only the HTTP routes left this socket as the way around them.

        The participant is real and invited - what they lack is the entitlement
        every game view enforces. Without this check they could still watch
        live rounds and the scoreboard and post chat while every HTTP route
        answered 403.
        """
        _run(self._a_participant_without_alpha_features_is_rejected())

    async def _a_participant_without_alpha_features_is_rejected(self) -> None:
        revoke = database_sync_to_async(UserSubscription.objects.filter(user=self.guest.user).delete)
        await revoke()
        comm = self._communicator(self.guest.user)
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_a_chat_message_is_broadcast_to_every_connected_participant(self) -> None:
        _run(self._a_chat_message_is_broadcast_to_every_connected_participant())

    async def _a_chat_message_is_broadcast_to_every_connected_participant(self) -> None:
        host_comm = self._communicator(self.host.user)
        connected, _ = await host_comm.connect()
        self.assertTrue(connected)
        guest_comm = self._communicator(self.guest.user)
        connected, _ = await guest_comm.connect()
        self.assertTrue(connected)

        # The consumer enqueues its broadcast rather than performing it (see
        # core.tests.celery_inline), so without this the group_send never happens.
        with broadcasts_delivered_inline():
            await host_comm.send_to(text_data=json.dumps({"body": "hello team"}))
            host_recv = json.loads(await host_comm.receive_from())
        guest_recv = json.loads(await guest_comm.receive_from())
        self.assertEqual(host_recv["type"], "chat.message")
        self.assertEqual(host_recv["message"]["body"], "hello team")
        self.assertEqual(guest_recv["message"]["body"], "hello team")
        self.assertEqual(host_recv["message"]["username"], self.host.username)

        await host_comm.disconnect()
        await guest_comm.disconnect()

    def test_a_blank_chat_message_is_silently_ignored(self) -> None:
        _run(self._a_blank_chat_message_is_silently_ignored())

    async def _a_blank_chat_message_is_silently_ignored(self) -> None:
        comm = self._communicator(self.host.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "   "}))
        self.assertTrue(await comm.receive_nothing(timeout=0.2))

        await comm.disconnect()

    def test_an_unparseable_frame_is_silently_ignored(self) -> None:
        _run(self._an_unparseable_frame_is_silently_ignored())

    async def _an_unparseable_frame_is_silently_ignored(self) -> None:
        comm = self._communicator(self.host.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data="not json at all")
        self.assertTrue(await comm.receive_nothing(timeout=0.2))

        await comm.disconnect()
