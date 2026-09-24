"""DM presence is per-connection membership that expires, not a counter that cannot (G2-28).

The counter had no expiry and a non-atomic first increment: two sockets opening together
both "created" it at 1, so closing one showed the other tab offline, and a worker that died
without running disconnect left the profile online for good.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import connection_registry
from urbanlens.dashboard.services.messaging.direct_messages import (
    is_profile_online,
    mark_profile_offline,
    mark_profile_online,
    refresh_profile_presence,
)
from urbanlens.dashboard.services.security import socket_budget

PROFILE = SimpleNamespace(pk=7)


class PresenceTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = FakeRedis()
        patch = mock.patch.object(connection_registry, "store_client", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)

    def _later(self, seconds: float):  # noqa: ANN202
        now = time.time() + seconds
        return mock.patch.object(connection_registry.time, "time", return_value=now)

    def test_a_connected_profile_is_online(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        self.assertTrue(is_profile_online(PROFILE))

    def test_closing_one_of_two_tabs_leaves_the_profile_online(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        mark_profile_online(PROFILE.pk, "chan-b")
        mark_profile_offline(PROFILE.pk, "chan-a")
        self.assertTrue(is_profile_online(PROFILE))
        mark_profile_offline(PROFILE.pk, "chan-b")
        self.assertFalse(is_profile_online(PROFILE))

    def test_a_socket_whose_worker_died_stops_counting(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        with self._later(socket_budget.STALE_AFTER_SECONDS + 1):
            self.assertFalse(is_profile_online(PROFILE), "a disconnect that never ran left the profile online for good")

    def test_the_heartbeat_keeps_a_live_socket_online(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        with self._later(socket_budget.REFRESH_INTERVAL_SECONDS):
            refresh_profile_presence(PROFILE.pk, "chan-a")
        with self._later(socket_budget.REFRESH_INTERVAL_SECONDS + socket_budget.STALE_AFTER_SECONDS - 1):
            self.assertTrue(is_profile_online(PROFILE))

    def test_the_heartbeat_restores_a_socket_that_missed_its_window(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        with self._later(socket_budget.STALE_AFTER_SECONDS + 1):
            is_profile_online(PROFILE)
            refresh_profile_presence(PROFILE.pk, "chan-a")
            self.assertTrue(is_profile_online(PROFILE))

    def test_the_key_expires_on_its_own(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        self.assertIn("ul_dm_online:7", self.store.ttls, "the presence key has no expiry")

    def test_an_unreachable_store_reads_offline_and_does_not_raise(self) -> None:
        with mock.patch.object(connection_registry, "store_client", return_value=None):
            mark_profile_online(PROFILE.pk, "chan-a")
            self.assertFalse(is_profile_online(PROFILE))

    def test_presence_does_not_spend_the_socket_allowance(self) -> None:
        mark_profile_online(PROFILE.pk, "chan-a")
        with mock.patch.object(socket_budget, "_client", return_value=self.store):
            self.assertEqual(socket_budget.open_count("user:7"), 0)
