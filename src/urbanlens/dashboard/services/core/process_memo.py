"""A value computed at most once per interval in this process, for answers too costly to rebuild per request."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _always(_value: object) -> bool:
    return True


class ProcessMemo[T]:
    """Holds one value for ``ttl_seconds``, shared by every thread in the process.

    Not the Django cache: the point is to skip work without adding a round trip, and each process answering from
    its own copy is fine for anything allowed to be ``ttl_seconds`` stale.
    """

    def __init__(self, ttl_seconds: float) -> None:
        """Create an empty memo.

        Args:
            ttl_seconds: How long a kept value is served before it is recomputed.
        """
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        # Boxed so that a held None is distinguishable from nothing held.
        self._held: tuple[T] | None = None
        self._expires_at = 0.0

    def _fresh(self) -> tuple[T] | None:
        held = self._held
        return held if held is not None and time.monotonic() < self._expires_at else None

    def get(self, compute: Callable[[], T], *, keep: Callable[[T], bool] = _always) -> T:
        """Return the held value, or compute one; concurrent callers wait for a single computation.

        Args:
            compute: Produces a fresh value. An exception propagates and nothing is held.
            keep: Whether a fresh value may be held; a failure sentinel should be recomputed next time instead.

        Returns:
            The held or freshly computed value.
        """
        if (held := self._fresh()) is not None:
            return held[0]
        with self._lock:
            if (held := self._fresh()) is not None:
                return held[0]
            value = compute()
            if keep(value):
                self._held = (value,)
                self._expires_at = time.monotonic() + self._ttl_seconds
            return value

    def clear(self) -> None:
        """Drop the held value so the next ``get`` recomputes."""
        with self._lock:
            self._held = None
            self._expires_at = 0.0
