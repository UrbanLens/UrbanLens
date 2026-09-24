"""Volume budgets for inbound WebSocket frames. Two tiers, because neither alone is enough:"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

from asgiref.sync import sync_to_async

from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage

#: Prefix for every cache counter this module writes, so they are greppable in
#: a cache dump and cannot collide with another feature's keys.
_KEY_PREFIX = "wsfreq"


@dataclass(frozen=True, slots=True)
class FrameBudget:
    """At most ``limit`` charges per ``window_seconds`` for one sender.

    Attributes:
        name: Distinguishes this budget's keys from every other budget's.
        limit: Charges allowed per window.
        window_seconds: Length of the fixed window.
        on_outage: What happens while the counter store is down. ``LOCAL`` by
            default: daphne is one process, so a local count is the same limit."""

    name: str
    limit: int
    window_seconds: int = 60
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
        try:
            return counters.hit(self._key(identity), self.window_seconds, on_outage=self.on_outage) <= self.limit
        except CounterUnavailableError:
            return False

    def refund(self, identity: str) -> None:
        """Give back one charge, for an event that turned out not to happen. Never below zero.

        Args:
            identity: The identity that was charged.
        """
        if self.limit <= 0:
            return
        counters.refund(self._key(identity))

    async def aconsume(self, identity: str) -> bool:
        """:meth:`consume`, called from the event loop."""
        return await sync_to_async(self.consume, thread_sensitive=False)(identity)


@dataclass(slots=True)
class ConnectionRate:
    """A fixed-window event counter private to one connection.
    Holds no cache and no lock: a consumer instance handles its own frames on one event loop, so a plain attribute is already serialised.

    Attributes:
        limit: Events allowed per window.
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
