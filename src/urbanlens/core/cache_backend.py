"""A cache that gives up quickly, and behaves like an empty one, when it is down.

Measured on a staging-model environment with Valkey paused (P105): every
request 500s **after about 32 seconds**, the readiness endpoint included. The
500 is not the interesting part - `socket_connect_timeout` is 1s and
`socket_timeout` 2s, so one call fails in about two. Reaching thirty-two means
roughly sixteen cache operations per request, each waiting its own timeout, one
after another. Whole-site throughput during a Valkey outage was about two
requests a second, and a readiness probe with any sane timeout records a
timeout rather than a verdict - so orchestration removes the instance that
could still have served from the database.

So the fix has two halves and the second is the one that matters:

1. **A cache that cannot be reached behaves like a cache with nothing in it.**
   Django already wraps `cached_db`'s `load()` and `save()` this way and leaves
   `exists()` and `delete()` bare, which is why logging in and out raise while
   browsing survives. Doing it in the backend covers those, and every caller in
   this codebase that reaches `cache.get` directly, in one place.
2. **The whole request pays one timeout, not one per call.** After a failure
   the breaker holds the cache open for a few seconds and every operation
   returns its empty-cache answer immediately, without touching a socket. One
   probe is let through when that window ends, so recovery needs no signal.

Two answers here are deliberately not "as if empty":

- `add` reports **False**. It is the claim half of every lock in this codebase
  (`services/core/single_flight.py`), and a lock handed out by a store that
  cannot hold it is not a lock. Callers using it as a cheap `set` merely think
  the key was already there.
- `incr` raises :class:`CacheUnavailableError`, a `ValueError` - what Django
  raises for a key that is not there - so a caller that restarts a window on a
  missing key still degrades, and a caller that must tell an outage from an
  absent key can.

Counters and locks do not go through the empty-cache answers at all: they use
the :class:`AtomicCacheOps` methods, which are single atomic operations and
raise :class:`CacheUnavailableError` instead of guessing. What a caller does
then is its own choice - see `services/core/counters.py`.
"""

from __future__ import annotations

import logging
import pickle
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache, RedisSerializer

if TYPE_CHECKING:
    from collections import OrderedDict
    from collections.abc import Callable, Iterable
    from threading import Lock

logger = logging.getLogger(__name__)

#: Raised when the store cannot be reached at all. **redis-py's exceptions do
#: not subclass the builtins** - its `ConnectionError` and `TimeoutError` derive
#: from `RedisError`, not from `OSError` - so catching only the builtins caught
#: nothing, and every unit test still passed because the mocks raised builtins.
#: Found by pausing Valkey against a real stack.
_UNREACHABLE: tuple[type[BaseException], ...] = (OSError,)

#: The store answered, and refused. A full Dragonfly (run without `cache_mode`,
#: deliberately - see docker-compose.yml) rejects a write with
#: `OutOfMemoryError` the same way Valkey's `volatile-lru` did; that is one
#: write not fitting, not the store going away, so it degrades the call
#: without holding reads off - reads are still perfectly able to answer.
_REFUSED: tuple[type[BaseException], ...] = ()

try:  # pragma: no cover - redis is a hard dependency of the backend this extends
    from redis.exceptions import ConnectionError as RedisConnectionError, OutOfMemoryError, TimeoutError as RedisTimeoutError

    _UNREACHABLE = (*_UNREACHABLE, RedisConnectionError, RedisTimeoutError)
    _REFUSED = (OutOfMemoryError,)
except ImportError:
    pass

#: Everything answered as an empty cache. Deliberately excludes `ValueError`,
#: which `incr` raises for an absent key - a fact about the data, not the
#: connection - and `ResponseError` generally, which is a real protocol error.
_DEGRADES: tuple[type[BaseException], ...] = (*_UNREACHABLE, *_REFUSED)


#: What :meth:`AtomicLocMemCache._live_value` answers for a key that holds nothing.
_ABSENT = object()


class CacheUnavailableError(ValueError):
    """The store could not be reached, or refused the write.

    A ``ValueError`` because that is what ``incr`` raises for an absent key, so
    existing callers of ``incr`` keep degrading; anything that has to tell the
    two apart catches this first.
    """


@runtime_checkable
class AtomicCacheOps(Protocol):
    """Single-step operations for counters and locks, which raise rather than answer as empty."""

    def incr_window(self, key: str, ttl: int, *, sliding: bool = False) -> int:
        """Increment *key*, creating it at 1 with a *ttl*-second expiry.

        Args:
            key: The counter's key.
            ttl: Seconds the counter lives.
            sliding: Restart the expiry on every increment rather than only on creation.

        Returns:
            The count including this increment.

        Raises:
            CacheUnavailableError: The store could not count it.
        """
        ...

    def peek_int(self, key: str) -> int:
        """The counter's value, 0 when absent.

        Raises:
            CacheUnavailableError: The store could not be read.
        """
        ...

    def decr_if_positive(self, key: str) -> None:
        """Decrement *key* unless it is absent or already 0.

        Raises:
            CacheUnavailableError: The store could not be reached.
        """
        ...

    def delete_if_value(self, key: str, value: str) -> bool:
        """Delete *key* only while it still holds *value*.

        Returns:
            Whether it was deleted.

        Raises:
            CacheUnavailableError: The store could not be reached.
        """
        ...


# KEYS[1] counter; ARGV[1] ttl seconds; ARGV[2] "1" to slide the expiry. A key with no
# expiry at all is given one, so a counter written by anything else cannot live forever.
_INCR_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or ARGV[2] == '1' or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

_DECR_IF_POSITIVE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current and current > 0 then
    return redis.call('DECR', KEYS[1])
end
return 0
"""

_DELETE_IF_VALUE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


#: How long one failure keeps the breaker open. Long enough that a request
#: making a dozen cache calls pays a single timeout; short enough that a Dragonfly
#: restart is noticed within a page load or two.
DEFAULT_BREAKER_SECONDS = 10.0


class ResilientRedisCache(RedisCache):
    """``RedisCache`` that degrades to an empty cache instead of raising.

    The breaker is per process, held in memory: it is a statement about what
    this worker just observed, and sharing it would need the store that is
    down. Workers therefore discover an outage independently, each at the cost
    of one timeout.
    """

    def __init__(self, server: str, params: dict[str, Any]) -> None:
        """Build the backend.

        Args:
            server: Connection URL, as Django passes it.
            params: Backend params. ``OPTIONS['BREAKER_SECONDS']`` overrides
                :data:`DEFAULT_BREAKER_SECONDS`; 0 disables the breaker, so
                every call tries the store and only the raising is suppressed.
        """
        # Removed before the superclass sees it: `RedisCache` forwards every
        # OPTIONS key to the redis client's constructor, so leaving ours there
        # would break the connection rather than configure the breaker.
        params = dict(params)
        options = dict(params.get("OPTIONS") or {})
        self._breaker_seconds = float(options.pop("BREAKER_SECONDS", DEFAULT_BREAKER_SECONDS))
        params["OPTIONS"] = options
        # Before the superclass runs, so `is_open` is answerable from the
        # moment this object exists.
        self._unreachable_until = 0.0
        super().__init__(server, params)

    @property
    def is_open(self) -> bool:
        """Whether the breaker is currently holding the store off."""
        return time.monotonic() < self._unreachable_until

    def _trip(self, operation: str, exc: BaseException) -> None:
        """Record a failure and start (or extend) the breaker window."""
        first = not self.is_open
        self._unreachable_until = time.monotonic() + self._breaker_seconds
        if first:
            # Once per window, and without the stack: the traceback is the same
            # every time and says less than the exception's own message, while
            # a long outage at one per window per worker is enough volume to
            # age real errors out of a rotated log (H61).
            logger.warning("Cache unreachable on %s (%s: %s); serving as empty for %.0fs", operation, type(exc).__name__, exc, self._breaker_seconds)

    def _guard(self, operation: str, call: Callable[[], Any], *, fallback: Any) -> Any:
        """Run *call*, answering *fallback* when the store cannot be reached.

        Args:
            operation: Name of the cache operation, for the log line.
            call: The superclass operation, already bound to its arguments.
            fallback: What an empty cache would have answered.

        Returns:
            The store's answer, or *fallback*.
        """
        if self.is_open:
            return fallback
        try:
            return call()
        except _DEGRADES as exc:
            if isinstance(exc, _UNREACHABLE):
                self._trip(operation, exc)
            else:
                logger.warning("Cache refused %s: %s", operation, exc)
            return fallback

    def get(self, key: str, default: Any = None, version: int | None = None) -> Any:
        return self._guard("get", lambda: super(ResilientRedisCache, self).get(key, default, version), fallback=default)

    def set(self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None) -> None:
        self._guard("set", lambda: super(ResilientRedisCache, self).set(key, value, timeout, version), fallback=None)

    def add(self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None) -> bool:
        # False, not True: this is the claim half of every lock here, and a
        # lock granted by a store that cannot hold it is not a lock.
        return bool(self._guard("add", lambda: super(ResilientRedisCache, self).add(key, value, timeout, version), fallback=False))

    def touch(self, key: str, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None) -> bool:
        return bool(self._guard("touch", lambda: super(ResilientRedisCache, self).touch(key, timeout, version), fallback=False))

    def delete(self, key: str, version: int | None = None) -> bool:
        return bool(self._guard("delete", lambda: super(ResilientRedisCache, self).delete(key, version), fallback=False))

    def has_key(self, key: str, version: int | None = None) -> bool:
        return bool(self._guard("has_key", lambda: super(ResilientRedisCache, self).has_key(key, version), fallback=False))

    def get_many(self, keys: Iterable[str], version: int | None = None) -> dict[str, Any]:
        return self._guard("get_many", lambda: super(ResilientRedisCache, self).get_many(keys, version), fallback={})

    def set_many(self, data: dict[str, Any], timeout: Any = DEFAULT_TIMEOUT, version: int | None = None) -> list[str]:
        # Django's contract is "the keys that failed", which with no store is
        # all of them.
        return self._guard("set_many", lambda: super(ResilientRedisCache, self).set_many(data, timeout, version), fallback=list(data))

    def delete_many(self, keys: Iterable[str], version: int | None = None) -> None:
        self._guard("delete_many", lambda: super(ResilientRedisCache, self).delete_many(keys, version), fallback=None)

    def clear(self) -> None:
        self._guard("clear", lambda: super(ResilientRedisCache, self).clear(), fallback=None)

    def incr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Increment, raising :class:`CacheUnavailableError` when the store cannot be reached.

        Raises:
            ValueError: The key is absent.
            CacheUnavailableError: The store is unreachable (a ``ValueError`` too).
        """
        return int(self._strict("incr", lambda: super(ResilientRedisCache, self).incr(key, delta, version)))

    def _strict(self, operation: str, call: Callable[[], Any]) -> Any:
        """Run *call*, raising :class:`CacheUnavailableError` rather than answering as empty.

        Args:
            operation: Name of the cache operation, for the log line.
            call: The store operation.

        Returns:
            The store's answer.

        Raises:
            CacheUnavailableError: The breaker is open, the store is unreachable, or it refused the write.
        """
        if self.is_open:
            raise CacheUnavailableError(f"Cache unreachable; {operation} not attempted")
        try:
            return call()
        except _DEGRADES as exc:
            if isinstance(exc, _UNREACHABLE):
                self._trip(operation, exc)
            raise CacheUnavailableError(f"Cache unreachable; {operation} failed") from exc

    def _eval(self, operation: str, script: str, key: str, *args: Any) -> Any:
        """Run a one-key Lua script against the key as Django names it."""
        full_key = self.make_and_validate_key(key)
        return self._strict(operation, lambda: self._cache.get_client(full_key, write=True).eval(script, 1, full_key, *args))

    def incr_window(self, key: str, ttl: int, *, sliding: bool = False) -> int:
        return int(self._eval("incr_window", _INCR_WINDOW_LUA, key, int(ttl), "1" if sliding else "0"))

    def peek_int(self, key: str) -> int:
        full_key = self.make_and_validate_key(key)
        raw = self._strict("peek_int", lambda: self._cache.get_client(full_key).get(full_key))
        if raw is None:
            return 0
        # A flag written through the plain cache API (``set(key, True)``) is pickled, not an integer.
        return int(RedisSerializer().loads(raw) or 0)

    def decr_if_positive(self, key: str) -> None:
        self._eval("decr_if_positive", _DECR_IF_POSITIVE_LUA, key)

    def delete_if_value(self, key: str, value: str) -> bool:
        # Compared as stored: Django pickles every non-int value, and one string
        # pickles to the same bytes under one protocol.
        return bool(self._eval("delete_if_value", _DELETE_IF_VALUE_LUA, key, RedisSerializer().dumps(value)))


class AtomicLocMemCache(LocMemCache):
    """``LocMemCache`` with :class:`AtomicCacheOps`, each operation under the backend's own lock.

    For the test suite and store-less development, so counters and locks take the
    same path there as against Dragonfly. It is never unreachable.
    """

    # Set by LocMemCache.__init__; declared because the stubs leave them out.
    _lock: Lock
    _cache: OrderedDict[str, bytes]
    _expire_info: dict[str, float | None]
    _max_entries: int

    def _live_value(self, full_key: str) -> Any:
        """The unpickled value, or ``_ABSENT`` when missing or expired. Call with the lock held."""
        expires = self._expire_info.get(full_key, -1)
        if expires is not None and expires <= time.time():
            self._cache.pop(full_key, None)
            self._expire_info.pop(full_key, None)
            return _ABSENT
        return pickle.loads(self._cache[full_key])  # noqa: S301 - our own pickled value

    def _store(self, full_key: str, value: Any) -> None:
        self._cache[full_key] = pickle.dumps(value, self.pickle_protocol)
        self._cache.move_to_end(full_key, last=False)

    def incr_window(self, key: str, ttl: int, *, sliding: bool = False) -> int:
        full_key = self.make_and_validate_key(key)
        with self._lock:
            current = self._live_value(full_key)
            if current is _ABSENT:
                while self._cache and len(self._cache) >= self._max_entries:
                    evicted, _value = self._cache.popitem()
                    self._expire_info.pop(evicted, None)
            count = 1 if current is _ABSENT else int(current) + 1
            self._store(full_key, count)
            if count == 1 or sliding or self._expire_info.get(full_key) is None:
                self._expire_info[full_key] = self.get_backend_timeout(ttl)
            return count

    def peek_int(self, key: str) -> int:
        return int(self.get(key) or 0)

    def decr_if_positive(self, key: str) -> None:
        full_key = self.make_and_validate_key(key)
        with self._lock:
            current = self._live_value(full_key)
            if current is not _ABSENT and int(current) > 0:
                self._store(full_key, int(current) - 1)

    def delete_if_value(self, key: str, value: str) -> bool:
        full_key = self.make_and_validate_key(key)
        with self._lock:
            if self._live_value(full_key) != value:
                return False
            self._cache.pop(full_key, None)
            self._expire_info.pop(full_key, None)
            return True
