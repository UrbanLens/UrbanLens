"""One upstream call for many identical asks.

The panels of one page, and the building pins of one site, ask an upstream the same question within seconds of
each other, each from its own task. The first caller makes the call and shares the answer through the shared
cache; the others wait briefly for it instead of making their own.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer; `RuntimeError` is the test suite's network guard.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)


def _shared(key: str) -> tuple[bool, Any]:
    """Whether an answer is stored under ``key``, and the answer; it is boxed so that None can be one."""
    try:
        stored = cache.get(key)
    except _CACHE_ERRORS:
        return False, None
    if isinstance(stored, tuple) and len(stored) == 1:
        return True, stored[0]
    return False, None


def _in_flight(key: str) -> bool:
    try:
        return cache.get(key) is not None
    except _CACHE_ERRORS:
        return False


def coalesced[T](key: str, compute: Callable[[], T], *, ttl: int, wait_seconds: float = 20.0, poll_seconds: float = 0.25) -> T:
    """``compute()``'s answer, shared with every caller asking under ``key`` within ``ttl`` seconds.

    A failure is not shared: the next caller makes its own attempt. A caller that finds the question already in
    flight waits up to ``wait_seconds`` for its answer, then asks for itself.

    Args:
        key: Names the question, including every parameter that changes its answer.
        compute: Makes the upstream call.
        ttl: Seconds the answer is shared for.
        wait_seconds: Longest wait for an answer another caller is fetching.
        poll_seconds: How often a waiting caller looks for it.

    Returns:
        The answer.
    """
    found, answer = _shared(key)
    if found:
        return answer

    flight = f"{key}:flight"
    try:
        token = acquire_lock(flight, int(wait_seconds) + 15)
    except _CACHE_ERRORS:
        token = None
    if token is None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and _in_flight(flight):
            time.sleep(poll_seconds)
            found, answer = _shared(key)
            if found:
                return answer
        found, answer = _shared(key)
        if found:
            return answer

    try:
        # The previous holder may have answered between the first look and taking the lock.
        found, answer = _shared(key)
        if found:
            return answer
        computed = compute()
        try:
            cache.set(key, (computed,), ttl)
        except _CACHE_ERRORS:
            logger.warning("Could not share the answer to %s", key, exc_info=True)
        return computed
    finally:
        if token is not None:
            try:
                release_lock(flight, token)
            except _CACHE_ERRORS:
                logger.warning("Could not release %s; it will expire", flight, exc_info=True)
