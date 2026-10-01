"""Chat budgets allow a burst of quick messages, then refill at a steady rate, instead of a fixed count per minute."""

from __future__ import annotations

import unittest
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.cache.backends.redis import RedisCacheClient
from model_bakery import baker

from urbanlens.core.cache_backend import ResilientRedisCache
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage
from urbanlens.dashboard.services.core.frame_limits import ConnectionRate, FrameBudget
from urbanlens.dashboard.services.core.message_limits import MessageRateLimitedError
from urbanlens.dashboard.services.messaging.direct_messages import create_direct_message
from urbanlens.dashboard.tests.hypothesis.test_atomic_counters import store_down

_START_US = 1_900_000_000_000_000


class _Clock:
    """Stands in for the counters' clock, so refill can be stepped instead of slept."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.now_us = _START_US
        patcher = mock.patch.object(counters, "_now_us", side_effect=lambda: self.now_us)
        patcher.start()
        test.addCleanup(patcher.stop)

    def advance(self, milliseconds: float) -> None:
        self.now_us += int(milliseconds * 1000)


class TokenBucketTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        counters.reset_local_fallback()
        self.addCleanup(counters.reset_local_fallback)
        self.clock = _Clock(self)

    def _take(self, on_outage: Outage = Outage.REFUSE) -> bool:
        return counters.take_token("k", interval_us=250_000, burst=3, on_outage=on_outage)

    def test_a_burst_is_allowed_at_once_and_no_more(self) -> None:
        self.assertEqual([self._take() for _ in range(4)], [True, True, True, False])

    def test_one_token_comes_back_per_interval(self) -> None:
        for _ in range(3):
            self._take()
        self.clock.advance(249)
        self.assertFalse(self._take())
        self.clock.advance(1)
        self.assertTrue(self._take())
        self.assertFalse(self._take())

    def test_a_long_rest_refills_to_the_burst_and_no_further(self) -> None:
        self._take()
        self.clock.advance(60_000)
        self.assertEqual([self._take() for _ in range(4)], [True, True, True, False])

    def test_a_returned_token_can_be_spent_again(self) -> None:
        for _ in range(3):
            self._take()
        counters.return_token("k", interval_us=250_000)
        self.assertTrue(self._take())
        self.assertFalse(self._take())

    def test_returning_to_a_full_bucket_does_not_raise_the_burst(self) -> None:
        counters.return_token("k", interval_us=250_000)
        self.assertEqual([self._take() for _ in range(4)], [True, True, True, False])

    def test_an_outage_still_limits_in_process(self) -> None:
        with store_down():
            self.assertEqual([self._take(Outage.LOCAL) for _ in range(4)], [True, True, True, False])
            self.clock.advance(250)
            self.assertTrue(self._take(Outage.LOCAL))

    def test_a_refusing_bucket_refuses_during_an_outage(self) -> None:
        with store_down(), self.assertRaises(CounterUnavailableError):
            self._take()


class FrameBudgetShapeTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.clock = _Clock(self)

    def test_a_budget_allows_its_burst_then_refills_at_its_rate(self) -> None:
        budget = FrameBudget(name="probe", limit=240, burst=30)
        self.assertEqual(sum(budget.consume("user:1") for _ in range(40)), 30)
        self.clock.advance(250)
        self.assertTrue(budget.consume("user:1"))
        self.assertFalse(budget.consume("user:1"))

    def test_the_burst_never_exceeds_one_windows_allowance(self) -> None:
        budget = FrameBudget(name="probe", limit=3, burst=30)
        self.assertEqual(sum(budget.consume("user:1") for _ in range(10)), 3)

    def test_without_a_burst_a_windows_allowance_is_available_at_once(self) -> None:
        budget = FrameBudget(name="probe", limit=5)
        self.assertEqual(sum(budget.consume("user:1") for _ in range(10)), 5)

    def test_a_connection_rate_has_the_same_shape(self) -> None:
        rate = ConnectionRate(limit=240, burst=30)
        with mock.patch("urbanlens.dashboard.services.core.frame_limits.time.monotonic", return_value=1000.0):
            self.assertEqual(sum(rate.consume() for _ in range(40)), 30)
        with mock.patch("urbanlens.dashboard.services.core.frame_limits.time.monotonic", return_value=1000.25):
            self.assertTrue(rate.consume())
            self.assertFalse(rate.consume())


class TheRedisBackendTakesATokenInOneScriptTests(SimpleTestCase):
    def test_take_token_is_one_script_on_the_prefixed_key(self) -> None:
        backend = ResilientRedisCache("redis://127.0.0.1:6379/0", {"OPTIONS": {}})
        client = mock.Mock()
        client.eval.return_value = 1
        with mock.patch.object(RedisCacheClient, "get_client", return_value=client):
            self.assertTrue(backend.take_token("k", now_us=5, interval_us=250_000, burst=30))
        script, numkeys, key, *args = client.eval.call_args.args
        self.assertIn("PX", script)
        self.assertEqual((numkeys, key, args), (1, backend.make_and_validate_key("k"), [5, 250_000, 30]))


class TheDefaultChatBudgetTests(TestCase):
    """Production's own numbers, not overridden: people who type in quick short bursts are not throttled."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.clock = _Clock(self)
        self.sender = Profile.objects.get(user=baker.make("auth.User"))
        self.recipient = Profile.objects.get(user=baker.make("auth.User"))
        Profile.objects.filter(pk=self.recipient.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
        self.recipient.refresh_from_db()

    def _send(self, body: str) -> None:
        create_direct_message(self.sender, self.recipient, body)

    def test_a_quick_burst_of_short_messages_all_go_through(self) -> None:
        for letter in "abcdefghijklmnopqrstuvwxyz👍🎉🙂!":
            self._send(letter)

    def test_four_messages_a_second_can_be_kept_up(self) -> None:
        for second in range(30):
            for index in range(4):
                self._send(f"{second}.{index}")
            self.clock.advance(1000)

    def test_a_sustained_flood_is_still_refused(self) -> None:
        with self.assertRaises(MessageRateLimitedError):
            for index in range(200):
                self._send(f"flood {index}")
                self.clock.advance(100)

    def test_the_socket_frame_budget_carries_the_message_burst_and_rate(self) -> None:
        self.assertGreaterEqual(settings.UL_WEBSOCKET_FRAME_BURST, settings.UL_MESSAGE_BURST + 2)
        typing_and_heartbeat = 20 + 2
        self.assertGreaterEqual(
            settings.UL_WEBSOCKET_FRAMES_PER_MINUTE, settings.UL_MESSAGES_PER_MINUTE + typing_and_heartbeat
        )
