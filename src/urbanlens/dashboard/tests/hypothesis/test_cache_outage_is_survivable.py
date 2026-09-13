"""A Valkey outage must cost one timeout per request, not one per cache call.

Measured with Valkey paused on a staging-model environment (P105): every
request 500s after **32 seconds**, `/health/ready` included. The 500 came from
the paths Django leaves unwrapped in `cached_db` - `exists()` and `delete()`,
so logging in and out raise while browsing survives. The 32 seconds came from
somewhere else entirely: `socket_timeout` is 2s and a request makes roughly
sixteen cache calls, each waiting its own timeout in turn.

So the status code and the duration are two defects, and only the second
decides whether the site is up. A wrapper that swallows the error still leaves
a request costing sixteen timeouts, and a readiness probe still records a
timeout rather than a verdict.

These tests assert the count of attempts reaching the store, not wall time -
a clock assertion on this host measures the host. One attempt per window is the
whole claim; the rest is arithmetic.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache.backends.redis import RedisCache
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    OutOfMemoryError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)

from urbanlens.core.cache_backend import ResilientRedisCache
from urbanlens.core.tests.testcase import SimpleTestCase

_SERVER = "redis://127.0.0.1:6379/0"

#: The classes a real outage raises, and the trap this file exists to close.
#: redis-py's ``ConnectionError`` and ``TimeoutError`` derive from
#: ``RedisError``, **not** from the builtins of the same name - so a first
#: version of this suite passed 18/18 against a backend that caught nothing,
#: because every mock raised a builtin. Every degradation case runs against all
#: of these.
OUTAGE_ERRORS = (
    RedisConnectionError("valkey is down"),
    RedisTimeoutError("Timeout reading from socket"),
    ConnectionError("socket refused"),
    TimeoutError("socket timed out"),
)


def _cache(**options) -> ResilientRedisCache:
    return ResilientRedisCache(_SERVER, {"OPTIONS": options})


class ADownCacheBehavesLikeAnEmptyOneTests(SimpleTestCase):
    """Every operation answers what it would answer with nothing stored."""

    def setUp(self) -> None:
        super().setUp()
        self.cache = _cache()

    def _down(self, method: str, error: BaseException | None = None):
        return mock.patch.object(RedisCache, method, side_effect=error or RedisConnectionError("valkey is down"))

    def test_every_error_a_real_outage_raises_is_handled(self) -> None:
        """The one that would have caught the first version of this backend."""
        for error in OUTAGE_ERRORS:
            with self.subTest(error=type(error).__module__ + "." + type(error).__name__):
                cache = _cache()
                with mock.patch.object(RedisCache, "get", side_effect=error):
                    self.assertIsNone(cache.get("k"))
                self.assertTrue(
                    cache.is_open, "the breaker did not trip, so the rest of the request pays a timeout each"
                )

    def test_a_full_store_degrades_the_write_without_holding_reads_off(self) -> None:
        """`volatile-lru` still answers reads when it is too full to accept a write."""
        cache = _cache()
        with mock.patch.object(RedisCache, "set", side_effect=OutOfMemoryError("OOM command not allowed")):
            self.assertIsNone(cache.set("k", "v", 60))

        self.assertFalse(cache.is_open, "a refused write is not the store going away")
        with mock.patch.object(RedisCache, "get", return_value="warm") as store:
            self.assertEqual(cache.get("k"), "warm")
        store.assert_called_once()

    def test_get_is_a_miss(self) -> None:
        with self._down("get"):
            self.assertIsNone(self.cache.get("k"))
            self.assertEqual(self.cache.get("k", "fallback"), "fallback")

    def test_set_is_a_no_op_rather_than_an_error(self) -> None:
        with self._down("set"):
            self.assertIsNone(self.cache.set("k", "v", 60))

    def test_has_key_is_false(self) -> None:
        """`cached_db.exists()` calls this, and Django does not guard it - it is why login raises."""
        with self._down("has_key"):
            self.assertFalse(self.cache.has_key("k"))

    def test_delete_is_false(self) -> None:
        """`cached_db.flush()` reaches this, and Django does not guard it - it is why logout raises."""
        with self._down("delete"):
            self.assertFalse(self.cache.delete("k"))

    def test_get_many_is_empty(self) -> None:
        with self._down("get_many"):
            self.assertEqual(self.cache.get_many(["a", "b"]), {})

    def test_set_many_reports_every_key_as_failed(self) -> None:
        with self._down("set_many"):
            self.assertEqual(sorted(self.cache.set_many({"a": 1, "b": 2}, 60)), ["a", "b"])

    def test_touch_and_clear_and_delete_many_do_not_raise(self) -> None:
        with self._down("touch"):
            self.assertFalse(self.cache.touch("k", 60))
        with self._down("clear"):
            self.assertIsNone(self.cache.clear())
        with self._down("delete_many"):
            self.assertIsNone(self.cache.delete_many(["a"]))

    def test_add_refuses_rather_than_granting_a_lock_it_cannot_hold(self) -> None:
        """`services/core/single_flight` claims with `add`; True here would let everyone through."""
        with self._down("add"):
            self.assertFalse(self.cache.add("lock", "1", 60))

    def test_incr_raises_the_absent_key_error_callers_already_handle(self) -> None:
        """`account._bump_counter` catches ValueError and restarts the window."""
        with self._down("incr"), self.assertRaises(ValueError):
            self.cache.incr("counter")


class TheWholeRequestPaysOneTimeoutTests(SimpleTestCase):
    """The half that decides whether the site is up rather than merely non-500."""

    def test_sixteen_calls_reach_the_store_once(self) -> None:
        cache = _cache()
        with mock.patch.object(RedisCache, "get", side_effect=RedisConnectionError("valkey is down")) as attempted:
            for _ in range(16):
                cache.get("k")

        self.assertEqual(attempted.call_count, 1, "each call waited its own timeout, which is the 32 seconds")

    def test_the_breaker_covers_every_operation_not_just_the_one_that_tripped_it(self) -> None:
        cache = _cache()
        with mock.patch.object(RedisCache, "get", side_effect=RedisConnectionError("down")):
            cache.get("k")
        with mock.patch.object(RedisCache, "set", side_effect=AssertionError("must not be reached")) as blocked:
            cache.set("k", "v", 60)

        blocked.assert_not_called()

    def test_one_call_is_let_through_once_the_window_ends(self) -> None:
        """Recovery needs no signal, so nothing has to notice Valkey came back."""
        cache = _cache(BREAKER_SECONDS=0.05)
        with mock.patch.object(RedisCache, "get", side_effect=RedisConnectionError("down")) as attempted:
            cache.get("k")
            cache.get("k")
            self.assertEqual(attempted.call_count, 1)

        with mock.patch("time.monotonic", return_value=cache._unreachable_until + 1):
            with mock.patch.object(RedisCache, "get", return_value="warm") as recovered:
                self.assertEqual(cache.get("k"), "warm")
            recovered.assert_called_once()

    def test_a_healthy_cache_is_not_short_circuited(self) -> None:
        """The negative half: without this, a backend that never calls the store passes above."""
        cache = _cache()
        with mock.patch.object(RedisCache, "get", return_value="warm") as store:
            for _ in range(3):
                self.assertEqual(cache.get("k"), "warm")

        self.assertEqual(store.call_count, 3)

    def test_a_zero_window_still_suppresses_the_error(self) -> None:
        """Turning the breaker off must not turn the degradation off with it."""
        cache = _cache(BREAKER_SECONDS=0)
        with mock.patch.object(RedisCache, "get", side_effect=RedisConnectionError("down")) as attempted:
            self.assertIsNone(cache.get("k"))
            self.assertIsNone(cache.get("k"))

        self.assertEqual(attempted.call_count, 2)


class OnlyConnectionFailuresAreSwallowedTests(SimpleTestCase):
    """A cache that hides real errors is worse than one that raises."""

    def test_a_missing_key_still_raises_from_incr(self) -> None:
        cache = _cache()
        with (
            mock.patch.object(RedisCache, "incr", side_effect=ValueError("key not found")),
            self.assertRaises(ValueError),
        ):
            cache.incr("absent")
        self.assertFalse(cache.is_open, "a missing key is not an outage and must not trip the breaker")

    def test_an_unexpected_error_propagates(self) -> None:
        cache = _cache()
        with mock.patch.object(RedisCache, "get", side_effect=KeyError("something else")), self.assertRaises(KeyError):
            cache.get("k")
        self.assertFalse(cache.is_open)

    def test_a_protocol_error_is_not_an_outage(self) -> None:
        """`ResponseError` is the store answering that the command was wrong."""
        cache = _cache()
        with (
            mock.patch.object(RedisCache, "get", side_effect=ResponseError("WRONGTYPE")),
            self.assertRaises(ResponseError),
        ):
            cache.get("k")
        self.assertFalse(cache.is_open)


class TheDeploymentUsesItTests(SimpleTestCase):
    """A backend nothing is configured to use protects nothing."""

    def test_the_configured_backend_is_the_resilient_one(self) -> None:
        """`settings/test.py` swaps in locmem, so this reads the real module.

        Skipped rather than passed when no Valkey is configured: there is then
        no cache to be down, and asserting on a branch that did not run would
        be a green light for a deployment this never checked.
        """
        import importlib

        from urbanlens.UrbanLens.settings import base

        if not getattr(base, "VALKEY_URL", ""):
            self.skipTest("no Valkey configured here, so base.py never took the branch that names a backend")

        configured = base.CACHES["default"]["BACKEND"]
        module_name, _, class_name = configured.rpartition(".")
        resolved = getattr(importlib.import_module(module_name), class_name)

        self.assertTrue(issubclass(resolved, ResilientRedisCache), f"{configured} does not degrade when Valkey is down")

    def test_the_breaker_option_does_not_reach_the_redis_client(self) -> None:
        """`RedisCache` forwards every OPTIONS key to the client's constructor."""
        from urbanlens.UrbanLens.settings import base

        if not getattr(base, "VALKEY_URL", ""):
            self.skipTest("no Valkey configured here")

        options = base.CACHES["default"]["OPTIONS"]
        self.assertIn("BREAKER_SECONDS", options, "the setting is not wired, so the breaker runs on its default")
        built = ResilientRedisCache(base.VALKEY_URL, {"OPTIONS": dict(options)})
        self.assertNotIn("BREAKER_SECONDS", built._options)
