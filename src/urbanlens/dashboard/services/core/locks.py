"""Overlap locks for scheduled sweeps.

Beat tasks guard themselves with ``cache.add(key, ..., ttl)`` so two ticks cannot
run the same sweep concurrently, then release in a ``finally``. Releasing
unconditionally is wrong once a run outlives its own TTL: the lock has already
expired and been re-acquired by the *next* tick, so the slow run's release
deletes a lock it no longer holds. The tick after that acquires immediately, and
exclusion degrades run by run instead of recovering.

That is reachable rather than theoretical - several sweeps do unbounded per-row
work with external I/O (``send_due_checkin_reminders`` sends SMTP inline for
every due check-in) under a fixed-length TTL, so runtime grows with the data
while the TTL does not.

These helpers stamp a unique token into the lock and delete it only if that token
is still there, so a run that overran releases nothing. The read-then-delete is
not atomic - Django's cache API exposes no compare-and-delete - but it closes the
window from "the whole sweep" down to the gap between two cache calls, and a
missed delete costs at most one skipped tick once the TTL lapses.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


def acquire_lock(key: str, timeout: int) -> str | None:
    """Take a named overlap lock.

    Args:
        key: Cache key identifying the sweep.
        timeout: Lock TTL in seconds. Should sit just under the task's beat
            interval, so a tick is never skipped by a lock the previous run has
            already finished with.

    Returns:
        An opaque token to hand to :func:`release_lock`, or ``None`` if another
        run holds the lock - in which case the caller must not do the work.
    """
    token = uuid4().hex
    return token if cache.add(key, token, timeout) else None


def release_lock(key: str, token: str | None) -> None:
    """Release a lock taken by :func:`acquire_lock`, if it is still ours.

    Args:
        key: The same key passed to :func:`acquire_lock`.
        token: The token it returned. ``None`` is a no-op, so callers that did
            not acquire can release unconditionally.
    """
    if token is None:
        return
    holder = cache.get(key)
    if holder == token:
        cache.delete(key)
    elif holder is None:
        # Already gone - the TTL expired and nobody took it, or this is a second
        # release of the same token. Nothing to do, and nothing worth warning
        # about: there is no other holder to protect.
        logger.debug("Lock %s was already released or expired before its holder released it", key)
    else:
        # Expired mid-run and someone else now holds it. Deleting here would hand
        # a third run the lock while the second is still working. This is the case
        # worth a warning - it means the work outran its TTL, so two runs
        # overlapped - and it stays a usable signal only while the merely-absent
        # case above does not also raise it.
        logger.warning("Sweep lock %s outlived its TTL; leaving the current holder's lock alone", key)


@contextmanager
def beat_lock(key: str, timeout: int) -> Iterator[bool]:
    """Hold a named overlap lock for the duration of the block.

    The preferred form for new code; :func:`acquire_lock`/:func:`release_lock`
    exist for callers whose control flow does not fit a ``with``.

    Args:
        key: Cache key identifying the sweep.
        timeout: Lock TTL in seconds.

    Yields:
        Whether this caller acquired the lock.
    """
    token = acquire_lock(key, timeout)
    try:
        yield token is not None
    finally:
        release_lock(key, token)
