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
- `incr` raises `ValueError`, which is what Django raises for a key that is not
  there. `account._bump_counter` already handles that by starting the window
  again, so a failed-login counter degrades instead of exploding.

**The abuse controls fail open, and that is the project's existing decision
rather than a new one.** `services/security/throttle.py` and
`services/security/socket_budget.py` both allow when they cannot read their
counter, on the reasoning that a Valkey outage which also locks everyone out is
strictly worse than one that merely stops counting. The login lockout in
`controllers/account.py` now inherits the same behaviour by the same argument.
The residual is real and worth stating: for the length of an outage, and only
then, failed-login counting stops.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from django.core.cache.backends.redis import RedisCache

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

#: Raised when the store cannot be reached at all. **redis-py's exceptions do
#: not subclass the builtins** - its `ConnectionError` and `TimeoutError` derive
#: from `RedisError`, not from `OSError` - so catching only the builtins caught
#: nothing, and every unit test still passed because the mocks raised builtins.
#: Found by pausing Valkey against a real stack.
_UNREACHABLE: tuple[type[BaseException], ...] = (OSError,)

#: The store answered, and refused. A full Valkey rejects a write with
#: `OutOfMemoryError`; that is one write not fitting, not the store going away,
#: so it degrades the call without holding reads off - `volatile-lru` is still
#: perfectly able to answer them.
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

#: How long one failure keeps the breaker open. Long enough that a request
#: making a dozen cache calls pays a single timeout; short enough that a Valkey
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

    def set(self, key: str, value: Any, timeout: Any = ..., version: int | None = None) -> None:
        self._guard("set", lambda: super(ResilientRedisCache, self).set(key, value, timeout, version), fallback=None)

    def add(self, key: str, value: Any, timeout: Any = ..., version: int | None = None) -> bool:
        # False, not True: this is the claim half of every lock here, and a
        # lock granted by a store that cannot hold it is not a lock.
        return bool(self._guard("add", lambda: super(ResilientRedisCache, self).add(key, value, timeout, version), fallback=False))

    def touch(self, key: str, timeout: Any = ..., version: int | None = None) -> bool:
        return bool(self._guard("touch", lambda: super(ResilientRedisCache, self).touch(key, timeout, version), fallback=False))

    def delete(self, key: str, version: int | None = None) -> bool:
        return bool(self._guard("delete", lambda: super(ResilientRedisCache, self).delete(key, version), fallback=False))

    def has_key(self, key: str, version: int | None = None) -> bool:
        return bool(self._guard("has_key", lambda: super(ResilientRedisCache, self).has_key(key, version), fallback=False))

    def get_many(self, keys: Iterable[str], version: int | None = None) -> dict[str, Any]:
        return self._guard("get_many", lambda: super(ResilientRedisCache, self).get_many(keys, version), fallback={})

    def set_many(self, data: dict[str, Any], timeout: Any = ..., version: int | None = None) -> list[str]:
        # Django's contract is "the keys that failed", which with no store is
        # all of them.
        return self._guard("set_many", lambda: super(ResilientRedisCache, self).set_many(data, timeout, version), fallback=list(data))

    def delete_many(self, keys: Iterable[str], version: int | None = None) -> None:
        self._guard("delete_many", lambda: super(ResilientRedisCache, self).delete_many(keys, version), fallback=None)

    def clear(self) -> None:
        self._guard("clear", lambda: super(ResilientRedisCache, self).clear(), fallback=None)

    def incr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Increment, raising ``ValueError`` when the store cannot be reached.

        The same thing Django raises for a key that is absent, which is the
        honest answer: nothing here can say the key exists. Callers that
        already handle a missing counter therefore handle an outage too.

        Raises:
            ValueError: The key is absent, or the store is unreachable.
        """
        if self.is_open:
            raise ValueError(f"Cache unreachable; {key!r} cannot be incremented")
        try:
            return super().incr(key, delta, version)
        except _DEGRADES as exc:
            if isinstance(exc, _UNREACHABLE):
                self._trip("incr", exc)
            raise ValueError(f"Cache unreachable; {key!r} cannot be incremented") from exc
