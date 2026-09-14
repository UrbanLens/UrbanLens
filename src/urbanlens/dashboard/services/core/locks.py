"""Overlap locks for scheduled sweeps.
Beat tasks guard themselves with ``cache.add(key, ..., ttl)`` so two ticks cannot run the same sweep concurrently, then release in a ``finally``."""

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
        timeout: Lock TTL in seconds.

    Returns:
        An opaque token to hand to :func:`release_lock`, or ``None`` if another run holds the lock - in which case the caller must not do the work."""
    token = uuid4().hex
    return token if cache.add(key, token, timeout) else None


def release_lock(key: str, token: str | None) -> None:
    """Release a lock taken by :func:`acquire_lock`, if it is still ours.

    Args:
        key: The same key passed to :func:`acquire_lock`.
        token: The token it returned."""
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
        # Expired mid-run and someone else now holds it.
        # Deleting here would hand a third run the lock while the second is still working.
        logger.warning("Sweep lock %s outlived its TTL; leaving the current holder's lock alone", key)


@contextmanager
def beat_lock(key: str, timeout: int) -> Iterator[bool]:
    """Hold a named overlap lock for the duration of the block.

    Args:
        key: Cache key identifying the sweep.
        timeout: Lock TTL in seconds.

    Yields:
        Whether this caller acquired the lock."""
    token = acquire_lock(key, timeout)
    try:
        yield token is not None
    finally:
        release_lock(key, token)
