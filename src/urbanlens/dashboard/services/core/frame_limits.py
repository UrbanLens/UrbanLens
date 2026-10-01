"""Volume budgets for inbound WebSocket frames. Two tiers, because neither alone is enough:"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

from asgiref.sync import sync_to_async

from urbanlens.core.cache_backend import next_arrival
from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage

#: Prefix for every cache counter this module writes, so they are greppable in
#: a cache dump and cannot collide with another feature's keys.
_KEY_PREFIX = "wsfreq"


def _bucket_shape(limit: int, window_seconds: float, burst: int) -> tuple[int, int]:
    """The refill interval in microseconds and the bucket size, capped at one window's allowance."""
    return int(window_seconds * 1_000_000) // limit, min(burst, limit) if burst > 0 else limit


@dataclass(frozen=True, slots=True)
class FrameBudget:
    """A token bucket per sender: ``burst`` charges at once, refilling at ``limit`` per ``window_seconds``.

    Attributes:
        name: Distinguishes this budget's keys from every other budget's.
        limit: Charges allowed per window, sustained.
        window_seconds: The period ``limit`` is counted over.
        burst: Charges allowed back to back from a rested bucket, at most ``limit``; 0 means ``limit``.
        on_outage: What happens while the counter store is down. ``LOCAL`` by
            default: daphne is one process, so a local count is the same limit."""

    name: str
    limit: int
    window_seconds: int = 60
    burst: int = 0
    on_outage: Outage = Outage.LOCAL

    def _key(self, identity: str) -> str:
        return f"{_KEY_PREFIX}:{self.name}:{identity}"

    def consume(self, identity: str) -> bool:
        """Charge one event against *identity*'s budget.

        Args:
            identity: Who to charge. Callers build this from ids they already
                hold, never from client-supplied text, so one sender cannot
                spend another's budget.

        Returns:
            True when the event is within budget, False once it is spent.
        """
        if self.limit <= 0:
            return True
        interval_us, burst = _bucket_shape(self.limit, self.window_seconds, self.burst)
        try:
            return counters.take_token(self._key(identity), interval_us=interval_us, burst=burst, on_outage=self.on_outage)
        except CounterUnavailableError:
            return False

    def refund(self, identity: str) -> None:
        """Give back one charge, for an event that turned out not to happen. Never past a full bucket.

        Args:
            identity: The identity that was charged.
        """
        if self.limit <= 0:
            return
        interval_us, _burst = _bucket_shape(self.limit, self.window_seconds, self.burst)
        counters.return_token(self._key(identity), interval_us=interval_us)

    async def aconsume(self, identity: str) -> bool:
        """:meth:`consume`, called from the event loop."""
        return await sync_to_async(self.consume, thread_sensitive=False)(identity)


@dataclass(slots=True)
class ConnectionRate:
    """:class:`FrameBudget`'s bucket, private to one connection.
    Holds no cache and no lock: a consumer instance handles its own frames on one event loop, so a plain attribute is already serialised.

    Attributes:
        limit: Events allowed per window, sustained.
        window_seconds: The period ``limit`` is counted over.
        burst: Events allowed back to back, at most ``limit``; 0 means ``limit``."""

    limit: int
    window_seconds: float = 60.0
    burst: int = 0
    _arrival_us: int = field(default=0, init=False)

    def consume(self) -> bool:
        """Charge one event; False while the bucket is empty."""
        if self.limit <= 0:
            return True
        interval_us, burst = _bucket_shape(self.limit, self.window_seconds, self.burst)
        arrival = next_arrival(self._arrival_us, int(time.monotonic() * 1_000_000), interval_us, burst)
        if arrival is None:
            return False
        self._arrival_us = arrival
        return True
