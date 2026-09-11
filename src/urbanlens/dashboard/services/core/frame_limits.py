"""Volume budgets for inbound WebSocket frames. Two tiers, because neither alone is enough:"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time

from asgiref.sync import sync_to_async
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Prefix for every cache counter this module writes, so they are greppable in
#: a cache dump and cannot collide with another feature's keys.
_KEY_PREFIX = "wsfreq"


def bump_window_counter(key: str, window_seconds: int) -> int:
    """Increment a fixed-window counter atomically and return its new value.
    ``incr`` is a single operation on both backends this project runs (Valkey's INCR, LocMemCache under its lock), so parallel frames cannot lose an increment the way a read-then-write would.

    Args:
        key: The counter's cache key.
        window_seconds: Seconds the window lasts.

    Returns:
        The count for the current window, including this event."""
    cache.add(key, 0, timeout=window_seconds)
    try:
        count = int(cache.incr(key))
    except ValueError:
        # Expired between the add and the incr; this event starts the window.
        cache.set(key, 1, timeout=window_seconds)
        return 1
    if count <= 1:
        cache.touch(key, timeout=window_seconds)
    return count


@dataclass(frozen=True, slots=True)
class FrameBudget:
    """At most ``limit`` charges per ``window_seconds`` for one sender.

    Attributes:
        name: Distinguishes this budget's keys from every other budget's.
        limit: Charges allowed per window. Zero or less disables the budget.
        window_seconds: Length of the fixed window.
    """

    name: str
    limit: int
    window_seconds: int = 60

    def consume(self, identity: str) -> bool:
        """Charge one event against *identity*'s budget.

        Args:
            identity: Who to charge. Callers build this from ids they already
                hold, never from client-supplied text, so one sender cannot
                spend another's budget.

        Returns:
            True when the event is within budget, False once it is spent. Also
            True when the cache is unavailable - see the module docstring for
            why this tier fails open and what still bounds a socket when it does.
        """
        if self.limit <= 0:
            return True
        try:
            return bump_window_counter(f"{_KEY_PREFIX}:{self.name}:{identity}", self.window_seconds) <= self.limit
        except Exception:
            logger.exception("Frame budget %s could not be read; allowing the frame", self.name)
            return True

    def refund(self, identity: str) -> None:
        """Give back one charge, for an event that turned out not to happen.
        A decrement can race the window rolling over, and the honest failure mode for a *refund* is being off by one in the sender's favour rather than holding a lock over a counter whose whole point is that it costs nothing.

        Args:
                identity: The identity that was charged."""
        if self.limit <= 0:
            return
        key = f"{_KEY_PREFIX}:{self.name}:{identity}"
        try:
            if int(cache.get(key) or 0) > 0:
                cache.decr(key)
        except Exception:
            logger.debug("Budget %s could not refund %s", self.name, identity, exc_info=True)

    async def aconsume(self, identity: str) -> bool:
        """:meth:`consume`, called from the event loop."""
        return await sync_to_async(self.consume, thread_sensitive=False)(identity)


@dataclass(slots=True)
class ConnectionRate:
    """A fixed-window event counter private to one connection.
    Holds no cache and no lock: a consumer instance handles its own frames on one event loop, so a plain attribute is already serialised.

    Attributes:
        limit: Events allowed per window. Zero or less disables the counter.
        window_seconds: Length of the fixed window."""

    limit: int
    window_seconds: float = 60.0
    _count: int = field(default=0, init=False)
    _window_started: float = field(default=0.0, init=False)

    def consume(self) -> bool:
        """Charge one event; False once this window's allowance is spent."""
        if self.limit <= 0:
            return True
        now = time.monotonic()
        if now - self._window_started >= self.window_seconds:
            self._window_started = now
            self._count = 0
        self._count += 1
        return self._count <= self.limit
