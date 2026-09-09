"""Volume budgets for inbound WebSocket frames.

Two tiers, because neither alone is enough:

- :class:`ConnectionRate` counts in the consumer's own process, off a monotonic
  clock. It bounds one socket, costs nothing, and cannot fail - which matters,
  because a frame flood is the most likely thing to make the *other* tier
  unavailable.
- :class:`FrameBudget` counts in the shared cache, keyed by who is sending. It
  bounds one identity across every tab and socket they have open, which the
  per-connection tier cannot see.

Distinct from :mod:`urbanlens.dashboard.services.core.rate_limiter`, which
budgets *outbound* calls to paid third-party APIs: that one is global per
service, backed by ``ApiCallLog`` COUNT queries, and fails closed because the
resource it guards is money. This one is per sender, backed by one cache
counter, and fails open - a cache blip must not silence chat. The per-connection
tier is what makes failing open acceptable: when the cache is unreachable every
individual socket is still bounded, so the worst case is a user with many tabs
briefly exceeding their share, not an unbounded flood.
"""

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

    ``incr`` is a single operation on both backends this project runs (Valkey's
    INCR, LocMemCache under its lock), so parallel frames cannot lose an
    increment the way a read-then-write would.

    Unlike the login lockout in ``controllers.account._bump_counter`` this does
    *not* touch the expiry on every call: the window has to end while the client
    is still sending, or a throttled client's own retries would hold it
    throttled forever.

    It does re-set the expiry on the first event of a window, and that is not
    redundant. Django's Redis backend implements ``incr`` as EXISTS followed by
    INCR (two round trips); when the key's TTL fires between them, INCR recreates
    it with no expiry at all, and Valkey runs ``--maxmemory-policy volatile-lru``,
    which never evicts a key without a TTL. The counter would then climb forever
    and that sender would be throttled until someone flushed the cache. A count
    of one means this call created the key - either normally or through that
    race - so stamping the expiry there repairs it without sliding the window.

    Args:
        key: The counter's cache key.
        window_seconds: Seconds the window lasts.

    Returns:
        The count for the current window, including this event.
    """
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

        Approximate on purpose. A decrement can race the window rolling over,
        and the honest failure mode for a *refund* is being off by one in the
        sender's favour rather than holding a lock over a counter whose whole
        point is that it costs nothing. Silent when the key is gone, which is
        what an expired window looks like.

        Args:
            identity: The identity that was charged.
        """
        if self.limit <= 0:
            return
        key = f"{_KEY_PREFIX}:{self.name}:{identity}"
        try:
            if int(cache.get(key) or 0) > 0:
                cache.decr(key)
        except Exception:
            logger.debug("Budget %s could not refund %s", self.name, identity, exc_info=True)

    async def aconsume(self, identity: str) -> bool:
        """:meth:`consume`, called from the event loop.

        ``thread_sensitive=False`` is deliberate. Django's async cache API is
        not a shortcut here: ``BaseCache.aincr`` is a non-atomic get-then-set
        that no backend overrides, and the other ``a*`` helpers are
        thread-sensitive wrappers that would serialise every frame's cache call
        onto one executor.
        """
        return await sync_to_async(self.consume, thread_sensitive=False)(identity)


@dataclass(slots=True)
class ConnectionRate:
    """A fixed-window event counter private to one connection.

    Holds no cache and no lock: a consumer instance handles its own frames on
    one event loop, so a plain attribute is already serialised. That is the
    point - this tier keeps working when the cache does not.

    Attributes:
        limit: Events allowed per window. Zero or less disables the counter.
        window_seconds: Length of the fixed window.
    """

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
