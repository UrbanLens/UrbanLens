"""Cache a proxied body only when it is small enough to be worth storing.

Four views proxy bytes from somewhere else and cache them so the next request
does not re-fetch: Google Photos previews, Immich thumbnails, and the two map
tile proxies. All four wrote whatever came back into the single 512MB Valkey
that also holds sessions, the Channels layer and the Celery broker, under
`volatile-lru` - so a large enough body does not merely waste space, it evicts
other people's sessions.

Three of the four ask the provider for a thumbnail, so an oversized body means
the provider ignored the request. That is the case this exists for: serve it,
decline to store it, and say so once in the log rather than silently filling the
instance everything else shares.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: Largest proxied body worth storing. A thumbnail is tens of kilobytes; this is
#: generous enough that a normal one always caches and a surprise never does.
MAX_CACHED_BODY_BYTES = 512 * 1024


def set_if_small(key: str, content: bytes, content_type: str, timeout: int, *, label: str, max_bytes: int | None = None) -> bool:
    """Store ``(content, content_type)`` under *key* unless it is too large.

    Args:
        key: Cache key.
        content: The proxied body.
        content_type: Its content type, stored alongside.
        timeout: Seconds to keep it.
        label: What is being cached, for the log line when it is refused.
        max_bytes: Largest body to store. Defaults to
            :data:`MAX_CACHED_BODY_BYTES`, read at call time rather than bound
            as the parameter's default - a module constant used as a default is
            fixed when the function is defined, which makes it impossible to
            configure or to override in a test.

    Returns:
        Whether it was stored. Callers serve the body either way - refusing to
        cache must never mean refusing to answer.
    """
    ceiling = MAX_CACHED_BODY_BYTES if max_bytes is None else max_bytes
    if len(content) > ceiling:
        logger.warning("%s was %d bytes, over the %d cache ceiling; serving it uncached.", label, len(content), ceiling)
        return False
    try:
        cache.set(key, (content, content_type), timeout)
    except _CACHE_ERRORS:
        logger.warning("%s could not be cached", label, exc_info=True)
        return False
    return True
