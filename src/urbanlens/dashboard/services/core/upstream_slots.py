"""How many upstream fetches one web process may have in flight for a given proxy.

A tile viewport is ~30 tiles and the browser asks for all of them at once, so on a cold cache a
proxy can hold every request thread in the process for as long as the upstream takes per tile -
measured at ~1.5s against REData (``P131``). Unbounded, one map load stalls the whole site.

Each proxy gets its own subclass and its own count. Sharing one would let an overlay nobody can
see starve the base layer under it, and the two upstreams fail independently anyway.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterator


class UpstreamSlots:
    """The process-wide bound for one proxy's upstream.

    Subclass it and implement :meth:`limit`. The semaphore is built on first use rather than at
    import, so a deployment - or a test - can set the count without the module having frozen it.
    """

    _semaphore: ClassVar[threading.BoundedSemaphore | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Give each subclass its own semaphore rather than the base class's.

        Args:
            **kwargs: Passed through to :class:`object`.
        """
        super().__init_subclass__(**kwargs)
        cls._semaphore = None
        cls._lock = threading.Lock()

    @classmethod
    def limit(cls) -> int:
        """How many fetches this proxy may have in flight at once.

        Returns:
            The count, read fresh each time the semaphore is built.

        Raises:
            NotImplementedError: Always, on the base class.
        """
        raise NotImplementedError

    @classmethod
    def semaphore(cls) -> threading.BoundedSemaphore:
        """The shared semaphore, built if this is the first call.

        Returns:
            This proxy's semaphore.
        """
        if cls._semaphore is None:
            with cls._lock:
                if cls._semaphore is None:
                    cls._semaphore = threading.BoundedSemaphore(cls.limit())
        return cls._semaphore

    @classmethod
    def reset(cls) -> None:
        """Drop the semaphore so the next call rereads the setting. Test-only."""
        with cls._lock:
            cls._semaphore = None

    @classmethod
    @contextlib.contextmanager
    def hold(cls) -> Iterator[bool]:
        """Hold one slot for the block, or yield ``False`` when none is free.

        Never blocks: a request thread waiting for a slot is occupying the resource the slot
        exists to ration, so over the cap the caller answers immediately instead.

        Yields:
            Whether a slot was taken.
        """
        acquired = cls.semaphore().acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                cls.semaphore().release()
