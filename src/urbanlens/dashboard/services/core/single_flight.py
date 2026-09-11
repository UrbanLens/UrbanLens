"""One expensive background job per account at a time.

"Scan your library" and "Export my data" each enqueued a full sweep on every
press, with nothing checking whether one was already running. Four presses meant
four concurrent sweeps, each able to hold one of only four worker slots for the
length of the task's hard limit - so one person's impatience became everyone
else's queue depth.

The claim is `cache.add`, which is atomic on every backend this deployment uses.
A read-then-write check would leave exactly the race a double-click produces.

It **fails closed**, the opposite of `services/security/throttle.py`, and for a
reason worth stating: a throttle that cannot read its counter should let the
request through, because refusing turns a cache outage into a lockout. A
single-flight guard that cannot read its counter should refuse, because
proceeding starts a second copy of the most expensive work in the application.
The asymmetry is deliberate. In practice the question rarely arises - the cache
and the Celery broker are the same Valkey, so a broker that cannot be read
cannot be enqueued to either.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer. `RuntimeError` is the
#: test suite's network guard.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: Stored while the job is being enqueued, before its task id is known. A caller
#: polling in that window sees "something is running", which is true.
PENDING = "pending"


def claim(key: str, ttl_seconds: int) -> bool:
    """Reserve *key* for one job, if nothing holds it.

    Args:
        key: Identifies the job and whose it is.
        ttl_seconds: How long the reservation survives without being released.
            Must exceed the task's own hard time limit, or a second copy can
            start while the first is still running.

    Returns:
        Whether the caller may start the job.
    """
    try:
        return bool(cache.add(key, PENDING, timeout=ttl_seconds))
    except _CACHE_ERRORS:
        logger.warning("single-flight %s could not be read; refusing to start a second copy", key, exc_info=True)
        return False


def adopt(key: str, token: str, ttl_seconds: int) -> None:
    """Record the task id now the job has one, keeping the reservation.

    Args:
        key: The reserved key.
        token: The task id to record.
        ttl_seconds: Reservation lifetime, reapplied.
    """
    try:
        cache.set(key, token, timeout=ttl_seconds)
    except _CACHE_ERRORS:
        logger.warning("single-flight %s could not record its task id", key, exc_info=True)


def holder(key: str) -> str | None:
    """What holds *key*, if anything.

    Args:
        key: The key to read.

    Returns:
        The task id, :data:`PENDING`, or None when nothing holds it.
    """
    try:
        return cache.get(key)
    except _CACHE_ERRORS:
        return None


def release(key: str) -> None:
    """Give up the reservation, so the next press may start a job.

    Args:
        key: The key to clear.
    """
    try:
        cache.delete(key)
    except _CACHE_ERRORS:
        logger.warning("single-flight %s could not be released; it will expire", key, exc_info=True)
