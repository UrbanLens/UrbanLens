"""One counter primitive, and a store outage that no longer removes the limits built on it (G2-5, G2-26, G2-27)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
import threading
from typing import TYPE_CHECKING
from unittest import mock

from django.core.cache import cache
from django.core.cache.backends.redis import RedisCacheClient
from django.test import RequestFactory
from django.urls import resolve, reverse
from redis.exceptions import ConnectionError as RedisConnectionError

from urbanlens.core.cache_backend import AtomicLocMemCache, CacheUnavailableError, ResilientRedisCache
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage
from urbanlens.dashboard.services.core.frame_limits import FrameBudget
from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
from urbanlens.dashboard.services.security import throttle

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def store_down() -> Iterator[None]:
    """Every counting path the store offers fails the way an unreachable Dragonfly does."""
    down = CacheUnavailableError("dragonfly is down")
    with ExitStack() as stack:
        for name in ("incr_window", "peek_int", "decr_if_positive", "delete_if_value"):
            stack.enter_context(mock.patch.object(AtomicLocMemCache, name, side_effect=down))
        stack.enter_context(mock.patch.object(AtomicLocMemCache, "incr", side_effect=down))
        stack.enter_context(mock.patch.object(AtomicLocMemCache, "add", return_value=False))
        stack.enter_context(
            mock.patch.object(AtomicLocMemCache, "get", side_effect=lambda key, default=None, version=None: default)
        )
        yield


class TheTestBackendIsAtomicTests(SimpleTestCase):
    def test_the_configured_backend_implements_the_atomic_operations(self) -> None:
        from django.core.cache import caches

        self.assertIsInstance(caches["default"], AtomicLocMemCache)

    def test_counts_up_and_reads_back_through_the_plain_cache_api(self) -> None:
        self.assertEqual([counters.hit("k", 60, on_outage=Outage.REFUSE) for _ in range(3)], [1, 2, 3])
        self.assertEqual(cache.get("k"), 3)
        self.assertEqual(counters.peek("k", on_outage=Outage.REFUSE), 3)

    def test_a_missing_key_reads_as_zero_not_as_an_outage(self) -> None:
        self.assertEqual(counters.peek("never-written", on_outage=Outage.REFUSE), 0)

    def test_refund_never_goes_below_zero(self) -> None:
        counters.hit("k", 60, on_outage=Outage.REFUSE)
        counters.refund("k")
        counters.refund("k")
        self.assertEqual(counters.peek("k", on_outage=Outage.REFUSE), 0)

    def test_parallel_hits_lose_nothing(self) -> None:
        threads = [
            threading.Thread(target=lambda: [counters.hit("k", 60, on_outage=Outage.REFUSE) for _ in range(50)])
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(counters.peek("k", on_outage=Outage.REFUSE), 400)


class AnOutageIsNotAMissingKeyTests(SimpleTestCase):
    """G2-27: an unreachable store read as "expired", so every event started a fresh window of one."""

    def test_refuse_raises_rather_than_counting_one(self) -> None:
        with store_down(), self.assertRaises(CounterUnavailableError):
            counters.hit("k", 60, on_outage=Outage.REFUSE)

    def test_local_keeps_counting_in_process(self) -> None:
        with store_down():
            self.assertEqual([counters.hit("k", 60, on_outage=Outage.LOCAL) for _ in range(3)], [1, 2, 3])
            self.assertEqual(counters.peek("k", on_outage=Outage.LOCAL), 3)

    def test_peek_refuses_rather_than_reading_zero(self) -> None:
        with store_down(), self.assertRaises(CounterUnavailableError):
            counters.peek("k", on_outage=Outage.REFUSE)

    def test_a_frame_budget_still_caps_frames_during_an_outage(self) -> None:
        budget = FrameBudget(name="probe", limit=3)
        with store_down():
            allowed = [budget.consume("user:1") for _ in range(10)]
        self.assertEqual(allowed.count(True), 3, "an outage removed the frame cap")

    def test_a_refusing_frame_budget_refuses_during_an_outage(self) -> None:
        budget = FrameBudget(name="probe", limit=3, on_outage=Outage.REFUSE)
        with store_down():
            self.assertFalse(budget.consume("user:1"))

    def test_a_throttle_still_limits_during_an_outage(self) -> None:
        rate = throttle.Rate(limit=2, window_seconds=60)
        with store_down():
            allowed = [throttle.allow("scope", "1.2.3.4", rate) for _ in range(5)]
        self.assertEqual(allowed, [True, True, False, False, False])

    def test_a_refusing_throttle_refuses_during_an_outage(self) -> None:
        rate = throttle.Rate(limit=2, window_seconds=60, on_outage=Outage.REFUSE)
        with store_down():
            self.assertFalse(throttle.allow("scope", "1.2.3.4", rate))


class TheLoginLockoutSurvivesAnOutageTests(TestCase):
    """Before: every failed login during an outage counted as the first, so none ever locked the account out."""

    def test_the_identifier_lockout_still_fires(self) -> None:
        from urbanlens.dashboard.controllers.account import _is_locked_out, _record_failed_attempt
        from urbanlens.dashboard.models.site_settings import SiteSettings

        limit = SiteSettings.get_current().login_max_attempts
        with store_down():
            for _ in range(limit):
                _record_failed_attempt("uid:1")
            self.assertTrue(_is_locked_out("uid:1"))

    def test_a_success_clears_the_local_count_too(self) -> None:
        from urbanlens.dashboard.controllers.account import (
            _clear_login_attempts,
            _is_locked_out,
            _record_failed_attempt,
        )
        from urbanlens.dashboard.models.site_settings import SiteSettings

        limit = SiteSettings.get_current().login_max_attempts
        with store_down():
            for _ in range(limit):
                _record_failed_attempt("uid:1")
            _clear_login_attempts("uid:1")
            self.assertFalse(_is_locked_out("uid:1"))


class TheRedisBackendDistinguishesAnOutageTests(SimpleTestCase):
    """The production backend: one Lua call per operation, raising rather than answering as empty."""

    def _cache(self) -> ResilientRedisCache:
        return ResilientRedisCache("redis://127.0.0.1:6379/0", {"OPTIONS": {}})

    def test_incr_window_is_one_script_on_the_prefixed_key(self) -> None:
        backend = self._cache()
        client = mock.Mock()
        client.eval.return_value = 4
        with mock.patch.object(RedisCacheClient, "get_client", return_value=client):
            self.assertEqual(backend.incr_window("k", 60, sliding=True), 4)
        script, numkeys, key, ttl, sliding = client.eval.call_args.args
        self.assertIn("INCR", script)
        self.assertEqual((numkeys, key, ttl, sliding), (1, backend.make_and_validate_key("k"), 60, "1"))

    def test_an_unreachable_store_raises_and_trips_the_breaker(self) -> None:
        backend = self._cache()
        client = mock.Mock()
        client.eval.side_effect = RedisConnectionError("dragonfly is down")
        with (
            mock.patch.object(RedisCacheClient, "get_client", return_value=client),
            self.assertRaises(CacheUnavailableError),
        ):
            backend.incr_window("k", 60)
        self.assertTrue(backend.is_open)
        with self.assertRaises(CacheUnavailableError):
            backend.peek_int("k")

    def test_a_missing_key_peeks_as_zero(self) -> None:
        backend = self._cache()
        client = mock.Mock()
        client.get.return_value = None
        with mock.patch.object(RedisCacheClient, "get_client", return_value=client):
            self.assertEqual(backend.peek_int("k"), 0)

    def test_a_flag_set_through_the_plain_api_peeks_as_one(self) -> None:
        from django.core.cache.backends.redis import RedisSerializer

        backend = self._cache()
        client = mock.Mock()
        client.get.return_value = RedisSerializer().dumps(True)
        with mock.patch.object(RedisCacheClient, "get_client", return_value=client):
            self.assertEqual(backend.peek_int("k"), 1)

    def test_delete_if_value_compares_the_stored_pickle(self) -> None:
        from django.core.cache.backends.redis import RedisSerializer

        backend = self._cache()
        client = mock.Mock()
        client.eval.return_value = 1
        with mock.patch.object(RedisCacheClient, "get_client", return_value=client):
            self.assertTrue(backend.delete_if_value("lock", "token"))
        self.assertEqual(client.eval.call_args.args[3], RedisSerializer().dumps("token"))

    def test_incr_still_raises_a_value_error_for_old_callers(self) -> None:
        self.assertTrue(issubclass(CacheUnavailableError, ValueError))


class LockReleaseIsCompareAndDeleteTests(SimpleTestCase):
    """G2-26: a release that read its own token, then lost the lock before deleting, deleted the next holder's."""

    _KEY = "urbanlens:test:cas-lock"

    def test_a_lock_retaken_between_read_and_delete_survives(self) -> None:
        slow = acquire_lock(self._KEY, 60)
        real_get = cache.get
        state = {"fast": None}

        def get_then_expire_and_retake(key, default=None, version=None):  # noqa: ANN001, ANN202
            value = real_get(key, default, version)
            if key == self._KEY and value == slow and state["fast"] is None:
                cache.delete(self._KEY)
                state["fast"] = acquire_lock(self._KEY, 60)
            return value

        with mock.patch.object(cache, "get", side_effect=get_then_expire_and_retake):
            release_lock(self._KEY, slow)

        fast = state["fast"] or acquire_lock(self._KEY, 60)
        self.assertIsNotNone(fast)
        self.assertEqual(real_get(self._KEY), fast, "the overrunning holder deleted its successor's lock")

    def test_releasing_the_current_token_frees_it(self) -> None:
        token = acquire_lock(self._KEY, 60)
        release_lock(self._KEY, token)
        self.assertIsNotNone(acquire_lock(self._KEY, 60))

    def test_an_outage_during_release_does_not_raise(self) -> None:
        token = acquire_lock(self._KEY, 60)
        with store_down():
            release_lock(self._KEY, token)


class TheHandRolledRouteCountersAreGoneTests(TestCase):
    """G2-5: get-then-set counters on two routes let a burst through at the boundary."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()

    def test_both_routes_are_throttled_in_the_urlconf(self) -> None:
        for name in ("suggest_passphrases", "validate_password_policy"):
            with self.subTest(name=name):
                self.assertTrue(getattr(resolve(reverse(name)).func, "throttle_scope", None), f"{name} has no throttle")

    def test_a_burst_at_the_boundary_admits_at_most_the_remaining_allowance(self) -> None:
        """Every request reads the counter before any writes it - the lost update, forced."""
        from urbanlens.dashboard.controllers.account import PASSPHRASE_SUGGEST_RATE

        view = resolve(reverse("suggest_passphrases")).func
        for _ in range(PASSPHRASE_SUGGEST_RATE.limit - 1):
            self.assertEqual(view(self.factory.get("/", REMOTE_ADDR="10.9.9.9")).status_code, 200)

        burst = 5
        arrived = threading.Barrier(burst, timeout=2)
        real_get = cache.get

        def read_together(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            value = real_get(*args, **kwargs)
            with suppress(threading.BrokenBarrierError):
                arrived.wait()
            return value

        statuses: list[int] = []
        with mock.patch.object(cache, "get", side_effect=read_together):
            threads = [
                threading.Thread(
                    target=lambda: statuses.append(view(self.factory.get("/", REMOTE_ADDR="10.9.9.9")).status_code)
                )
                for _ in range(burst)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(
            statuses.count(200), 1, f"a burst of {burst} at the boundary got {statuses.count(200)} through"
        )
