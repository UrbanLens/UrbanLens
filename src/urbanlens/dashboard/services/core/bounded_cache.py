"""Cache a proxied body only when it is small enough to be worth storing."""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: generous enough that a normal one always caches and a surprise never does.
MAX_CACHED_BODY_BYTES = 512 * 1024


def set_if_small(key: str, content: bytes, content_type: str, timeout: int, *, label: str, max_bytes: int = MAX_CACHED_BODY_BYTES) -> bool:
    """Store ``(content, content_type)`` under *key* unless it is too large.

    Args:
        key: Cache key.
        content: The proxied body.
        content_type: Its content type, stored alongside.
        timeout: Seconds to keep it.
        label: What is being cached, for the log line when it is refused.
        max_bytes: Largest body to store.

    Returns:
        Whether it was stored. Callers serve the body either way - refusing to
        cache must never mean refusing to answer.
    """
    if len(content) > max_bytes:
        logger.warning("%s was %d bytes, over the %d cache ceiling; serving it uncached.", label, len(content), max_bytes)
        return False
    try:
        cache.set(key, (content, content_type), timeout)
    except _CACHE_ERRORS:
        logger.warning("%s could not be cached", label, exc_info=True)
        return False
    return True
