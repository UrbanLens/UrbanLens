"""Atomic windowed counters, and what each caller does when the store cannot count.

Every rate limit, lockout and volume budget here is one of these: a key that counts
events and expires with its window. They share this module so that the increment is
atomic everywhere (one Lua call on Dragonfly, one locked step in the test backend)
and so that a store outage is a decision each caller makes, not an accident of which
exception it happened to catch.

A caller picks an :class:`Outage` policy:

- ``REFUSE`` raises :class:`CounterUnavailableError`, and the caller refuses the action.
  For limits that guard someone else's budget - an upstream we pay for, or share.
- ``LOCAL`` counts in this process instead, so the limit still holds per process for
  the length of the outage. For limits that guard our own capacity or an account,
  where refusing everyone would turn a cache outage into a site outage.

Neither lets an outage remove a limit.
"""

from __future__ import annotations

from collections import OrderedDict
from enum import StrEnum
import logging
import threading
import time
from typing import TYPE_CHECKING

from django.core.cache import DEFAULT_CACHE_ALIAS, caches

from urbanlens.core.cache_backend import AtomicCacheOps, CacheUnavailableError

if TYPE_CHECKING:
    from django.core.cache.backends.base import BaseCache

logger = logging.getLogger(__name__)

#: What a store can raise when it cannot answer; the builtins cover backends
#: other than ours and tests that simulate an outage.
_UNAVAILABLE = (CacheUnavailableError, ConnectionError, OSError)

#: Local windows kept per process during an outage. Bounded so an outage under a
#: flood of distinct identities costs memory in proportion to this, not to the flood.
_LOCAL_MAX_KEYS = 10_000

#: Seconds between outage warnings from this module, per process.
_WARN_INTERVAL_SECONDS = 60.0


class Outage(StrEnum):
    """What a counter does when the store cannot count."""

    REFUSE = "refuse"
    LOCAL = "local"


class CounterUnavailableError(RuntimeError):
    """The store could not count, and the caller chose :attr:`Outage.REFUSE`."""


class _GenericOps:
    """:class:`AtomicCacheOps` over any Django cache, for backends that lack them.

    Increments stay atomic (``add`` then ``incr``); the compare-and-delete does not,
    which is why the configured backends implement the protocol themselves.
    """

    def __init__(self, backend: BaseCache) -> None:
        self._backend = backend

    def incr_window(self, key: str, ttl: int, *, sliding: bool = False) -> int:
        for _attempt in range(2):
            if self._backend.add(key, 1, timeout=ttl):
                return 1
            try:
                count = int(self._backend.incr(key))
            except CacheUnavailableError:
                raise
            except ValueError:
                continue  # expired between the add and the incr
            if sliding:
                self._backend.touch(key, timeout=ttl)
            return count
        raise CacheUnavailableError(f"{key!r} could not be counted")

    def peek_int(self, key: str) -> int:
        return int(self._backend.get(key) or 0)

    def decr_if_positive(self, key: str) -> None:
        if int(self._backend.get(key) or 0) > 0:
            self._backend.decr(key)

    def delete_if_value(self, key: str, value: str) -> bool:
        if self._backend.get(key) != value:
            return False
        return bool(self._backend.delete(key))


class _LocalWindows:
    """Per-process fixed windows, used only while the store is unreachable."""

    def __init__(self, max_keys: int = _LOCAL_MAX_KEYS) -> None:
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def _live(self, key: str, now: float) -> tuple[int, float] | None:
        entry = self._entries.get(key)
        if entry is None or entry[1] <= now:
            self._entries.pop(key, None)
            return None
        return entry

    def hit(self, key: str, ttl: int, *, sliding: bool) -> int:
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            count = 1 if entry is None else entry[0] + 1
            expires = now + ttl if entry is None or sliding else entry[1]
            self._entries[key] = (count, expires)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_keys:
                self._entries.popitem(last=False)
            return count

    def peek(self, key: str) -> int:
        with self._lock:
            entry = self._live(key, time.monotonic())
            return 0 if entry is None else entry[0]

    def refund(self, key: str) -> None:
        with self._lock:
            entry = self._live(key, time.monotonic())
            if entry is not None and entry[0] > 0:
                self._entries[key] = (entry[0] - 1, entry[1])

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)


_local = _LocalWindows()
_last_warning = 0.0


def _ops() -> AtomicCacheOps:
    backend = caches[DEFAULT_CACHE_ALIAS]
    if isinstance(backend, AtomicCacheOps):
        return backend
    return _GenericOps(backend)


def _warn_outage(operation: str, key: str, on_outage: Outage) -> None:
    global _last_warning  # noqa: PLW0603 - one per-process rate limit on a log line
    now = time.monotonic()
    if now - _last_warning >= _WARN_INTERVAL_SECONDS:
        _last_warning = now
        logger.warning("Counter store unavailable on %s of %s; policy %s", operation, key, on_outage)


def hit(key: str, ttl: int, *, on_outage: Outage, sliding: bool = False) -> int:
    """Count one event and return the window's total, this one included.

    Args:
        key: The counter's key, including whatever window index the caller keys by.
        ttl: Seconds the window lasts.
        on_outage: What to do when the store cannot count.
        sliding: Restart the expiry on every event, so the counter lives *ttl*
            seconds past the last one rather than past the first.

    Returns:
        The count.

    Raises:
        CounterUnavailableError: The store could not count and *on_outage* is ``REFUSE``.
    """
    try:
        return _ops().incr_window(key, ttl, sliding=sliding)
    except _UNAVAILABLE as exc:
        _warn_outage("hit", key, on_outage)
        if on_outage is Outage.REFUSE:
            raise CounterUnavailableError(key) from exc
        return _local.hit(key, ttl, sliding=sliding)


def peek(key: str, *, on_outage: Outage) -> int:
    """The window's count so far, without counting.

    Args:
        key: The counter's key.
        on_outage: What to do when the store cannot be read.

    Returns:
        The count, 0 when there is none.

    Raises:
        CounterUnavailableError: The store could not be read and *on_outage* is ``REFUSE``.
    """
    try:
        return _ops().peek_int(key)
    except _UNAVAILABLE as exc:
        _warn_outage("peek", key, on_outage)
        if on_outage is Outage.REFUSE:
            raise CounterUnavailableError(key) from exc
        return _local.peek(key)


def refund(key: str) -> None:
    """Give back one event that turned out not to happen. Best effort, never below 0.

    Args:
        key: The counter's key.
    """
    try:
        _ops().decr_if_positive(key)
    except _UNAVAILABLE:
        _local.refund(key)


def clear(key: str) -> None:
    """Forget a counter, in the store and in the local fallback.

    Args:
        key: The counter's key.
    """
    _local.clear(key)
    try:
        caches[DEFAULT_CACHE_ALIAS].delete(key)
    except _UNAVAILABLE:
        logger.debug("Counter %s could not be cleared from the store", key)


def delete_if_value(key: str, value: str) -> bool:
    """Delete *key* only while it still holds *value*, atomically on the configured backends.

    Args:
        key: The key.
        value: The value it must still hold.

    Returns:
        Whether it was deleted.

    Raises:
        CacheUnavailableError: The store could not be reached.
    """
    return _ops().delete_if_value(key, value)


def reset_local_fallback() -> None:
    """Drop every local window. For tests, which share one process."""
    _local.clear()
