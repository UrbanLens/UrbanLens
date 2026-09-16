"""A socket's allowance lives in Dragonfly, so checking it has no business on the thread every socket's database work shares.

A notification socket opens on every page view and closes on the next, so its claim and release are among the busiest
calls daphne makes. On asgiref's one thread-sensitive executor they queue behind every other socket's user and profile
lookups, and a handshake that waits past daphne's five seconds is reset.
"""

from __future__ import annotations

import threading
from unittest import mock

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from model_bakery import baker

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.dashboard.consumers import UserNotificationConsumer
from urbanlens.dashboard.services.security import socket_budget


@database_sync_to_async
def _shared_db_thread() -> int:
    return threading.get_ident()


class TheAllowanceIsCheckedOffTheSharedThreadTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.store = FakeRedis()
        patch = mock.patch.object(socket_budget, "_client", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)

    async def _open_and_close(self) -> None:
        communicator = WebsocketCommunicator(UserNotificationConsumer.as_asgi(), "/ws/notifications/")
        communicator.scope["user"] = self.user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        self.assertEqual(socket_budget.open_count(f"user:{self.user.pk}"), 1, "the open socket was not counted")
        await communicator.disconnect()

    def test_claim_and_release_run_off_the_shared_database_thread(self) -> None:
        threads: dict[str, list[int]] = {"claim": [], "release": []}

        def recording(name: str):
            original = getattr(socket_budget, name)

            def call(*args, **kwargs):
                threads[name].append(threading.get_ident())
                return original(*args, **kwargs)

            return call

        with (
            mock.patch.object(socket_budget, "claim", recording("claim")),
            mock.patch.object(socket_budget, "release", recording("release")),
        ):
            async_to_sync(self._open_and_close)()
        shared = async_to_sync(_shared_db_thread)()

        self.assertEqual([len(threads["claim"]), len(threads["release"])], [1, 1])
        self.assertNotIn(shared, threads["claim"] + threads["release"], "a Dragonfly call queued behind database work")
        self.assertEqual(socket_budget.open_count(f"user:{self.user.pk}"), 0, "the closed socket kept its place")
