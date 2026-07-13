"""Tests for SafetyCheckinChatConsumer - real-time chat for safety check-ins.

Uses TransactionTestCase (not the project's default TestCase) because Channels
consumers touch the database from a background thread via
``database_sync_to_async`` - Channels' own testing docs call out
TransactionTestCase as the safe choice for exactly this reason. CHANNEL_LAYERS
is overridden to the in-memory backend so these tests don't need a real
Valkey/Redis connection.
"""

from __future__ import annotations

import json
import uuid

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer

_IN_MEMORY_CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _run(coro):
    """Run *coro* via async_to_sync, not a bare asyncio.run().

    database_sync_to_async's thread-sensitive mode needs the
    CurrentThreadExecutor that only async_to_sync's sync->async->sync bridge
    sets up; a coroutine driven by plain asyncio.run() has nothing pumping
    that queue, so any consumer DB access (e.g. SafetyCheckinChatConsumer's
    _resolve()/_create_message()) hangs forever instead of completing.
    """

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class SafetyCheckinChatConsumerTests(TransactionTestCase):
    """SafetyCheckinChatConsumer.connect()/receive() over the owner and contact routes."""

    def setUp(self):
        self.owner_user = baker.make("auth.User")
        self.owner_profile = self.owner_user.profile
        self.checkin = baker.make("dashboard.SafetyCheckin", profile=self.owner_profile)
        self.contact = baker.make(
            "dashboard.SafetyCheckinContact",
            checkin=self.checkin,
            contact_profile=None,
            email="contact@example.com",
        )

    def _owner_communicator(self) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/checkin/{self.checkin.uuid}/chat/")
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = self.owner_user
        return comm

    def _contact_communicator(self, token) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/contact/{token}/chat/")
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": None, "token": str(token)}}
        comm.scope["user"] = AnonymousUser()
        return comm

    def test_owner_and_contact_exchange_messages(self):
        _run(self._owner_and_contact_exchange_messages())

    async def _owner_and_contact_exchange_messages(self):
        owner_comm = self._owner_communicator()
        connected, _ = await owner_comm.connect()
        self.assertTrue(connected)

        contact_comm = self._contact_communicator(self.contact.token)
        connected, _ = await contact_comm.connect()
        self.assertTrue(connected)

        await owner_comm.send_to(text_data=json.dumps({"body": "On my way back"}))

        owner_echo = json.loads(await owner_comm.receive_from())
        contact_recv = json.loads(await contact_comm.receive_from())
        self.assertEqual(owner_echo["body"], "On my way back")
        self.assertEqual(contact_recv["body"], "On my way back")
        self.assertEqual(owner_echo["sender_name"], self.owner_profile.username)

        await owner_comm.disconnect()
        await contact_comm.disconnect()

    def test_invalid_token_is_rejected(self):
        _run(self._invalid_token_is_rejected())

    async def _invalid_token_is_rejected(self):
        comm = self._contact_communicator(uuid.uuid4())
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_unauthenticated_owner_route_is_rejected(self):
        _run(self._unauthenticated_owner_route_is_rejected())

    async def _unauthenticated_owner_route_is_rejected(self):
        comm = self._owner_communicator()
        comm.scope["user"] = AnonymousUser()
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_blank_message_is_silently_ignored(self):
        _run(self._blank_message_is_silently_ignored())

    async def _blank_message_is_silently_ignored(self):
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "   "}))
        self.assertTrue(await comm.receive_nothing(timeout=0.2))

        await comm.disconnect()

    def test_oversized_message_gets_error_frame(self):
        _run(self._oversized_message_gets_error_frame())

    async def _oversized_message_gets_error_frame(self):
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps({"body": "x" * 5000}))
        reply = json.loads(await comm.receive_from())
        self.assertEqual(reply["type"], "error")
        self.assertIn("too long", reply["detail"])

        await comm.disconnect()
