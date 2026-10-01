"""Cross-request caching for a value whose invalidating events are known by name.

A value read on every page and changed only by a rare, nameable event should not be recomputed
per request - and should not be left to a timeout either, because the gap between the event and
the timeout is a gap of wrong answers. A generation counter closes it: a reader takes the counter
and its entries in one round trip and ignores anything stamped with an older generation, and the
event increments the counter, which retires every entry in the namespace at once without anyone
having to know their keys.

The timeout stays, as the backstop for the case the counter cannot cover - a bump made while the
store is unreachable leaves entries written before the event readable until they expire. Choose
it for how long a wrong answer can be tolerated during an outage, not for how often the value
changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The counter's own entry never expires; nothing is derived from it, and losing it only costs a
#: recompute.
_FOREVER = None


class VersionedCache:
    """Entries in one namespace, retired together by a counter rather than one key at a time.

    Attributes:
        namespace: Prefix for every key, including the counter's.
        seconds: How long an entry survives without a bump.
    """

    __slots__ = ("_counter", "namespace", "seconds")

    def __init__(self, namespace: str, *, seconds: int) -> None:
        """Build a namespace.

        Args:
            namespace: Prefix for every key this instance reads or writes.
            seconds: Entry timeout, the backstop for a bump lost to an outage.
        """
        self.namespace = namespace
        self.seconds = seconds
        self._counter = f"{namespace}:generation"

    def _key(self, name: str) -> str:
        return f"{self.namespace}:{name}"

    def read(self, *names: str) -> tuple[int | None, dict[str, Any]]:
        """The current generation and whichever of *names* is stored under it.

        Args:
            *names: Entry names within this namespace.

        Returns:
            The generation, or None when the store could not answer, and the entries that carry
            it. An entry written under an older generation is absent rather than returned stale.
        """
        if not names:
            return None, {}
        wanted = {self._key(name): name for name in names}
        found = cache.get_many([self._counter, *wanted])
        generation = found.get(self._counter)
        if not isinstance(generation, int):
            return None, {}
        fresh: dict[str, Any] = {}
        for key, name in wanted.items():
            stored = found.get(key)
            if isinstance(stored, tuple) and len(stored) == 2 and stored[0] == generation:
                fresh[name] = stored[1]
        return generation, fresh

    def write(self, generation: int | None, values: Mapping[str, Any]) -> None:
        """Store *values* stamped with *generation*.

        Args:
            generation: The generation :meth:`read` reported, or None to establish one. A stamp
                that is already stale is written anyway and simply never read back - which is the
                whole point of stamping rather than deleting.
            values: Entry name to value. Values must survive the cache's own serialisation.
        """
        if not values:
            return
        if generation is None:
            generation = self.generation()
        if generation is None:
            return
        cache.set_many({self._key(name): (generation, value) for name, value in values.items()}, self.seconds)

    def generation(self) -> int | None:
        """The current generation, starting one if the namespace has never had a reader.

        Args:
            None.

        Returns:
            The generation, or None when the store could not answer - in which case the caller
            should compute its value and not cache it.
        """
        current = cache.get_or_set(self._counter, 1, _FOREVER)
        return current if isinstance(current, int) else None

    def bump(self, **_kwargs: object) -> None:
        """Retire every entry in this namespace.

        Shaped to be connected directly as a signal receiver, so the event that changes the value
        is the thing that invalidates it, with no per-key bookkeeping in between.

        Args:
            **_kwargs: Whatever the signal sent, ignored.
        """
        try:
            cache.incr(self._counter)
        except ValueError:
            # No counter yet, so there is nothing stamped with one to retire. Starting it at 1 is
            # the only branch that may write this key directly: any other recovery would risk
            # lowering the counter, which would revive entries rather than retire them.
            cache.set(self._counter, 1, _FOREVER)
