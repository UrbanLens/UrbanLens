"""Keep the cache from being the reason a request fails, or the reason it costs everyone.

Four views proxy bytes from somewhere else and cache them so the next request
does not re-fetch: Google Photos previews, Immich thumbnails, and the two map
tile proxies. What they store is keyed by whatever anyone asked for - a tile per
layer and coordinate - so the keyspace is bounded by nothing this deployment
controls, which is why it is not kept beside the sessions and the Channels
layer. Everything here reads and writes ``settings.PROXIED_BYTES_CACHE``, an
instance that may evict its least recently used bytes; a full one is a cache
miss rather than a failed session write for someone else.

Three of the four ask the provider for a thumbnail, so an oversized body means
the provider ignored the request. That is the case this exists for: serve it,
decline to store it, and say so once in the log rather than silently filling the
instance everything else shares.

The three plain wrappers below exist for the other half of that: a bare
``cache.set`` against a full or unreachable Dragonfly **raises**, so a cache
problem becomes a 500 on whatever the caller was answering. Every caller here
can answer without the cache - that is what a cache is - so none of them should
be able to fail because of it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.cache import caches

if TYPE_CHECKING:
    from django.core.cache.backends.base import BaseCache

logger = logging.getLogger(__name__)


def _store() -> BaseCache:
    """The cache these bytes belong in.

    Resolved per call rather than at import: ``caches`` is keyed off settings, and a test that
    overrides them has to be able to change where this writes.

    Returns:
        The proxied-bytes cache.
    """
    return caches[settings.PROXIED_BYTES_CACHE]


#: Anything the cache can raise when it cannot answer.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: generous enough that a normal one always caches and a surprise never does.
MAX_CACHED_BODY_BYTES = 512 * 1024


def get_or_none(key: str, *, label: str) -> Any | None:
    """Read *key*, treating a cache that cannot answer as a miss.

    Args:
        key: Cache key.
        label: What is being read, for the log line when the cache is down.

    Returns:
        The cached value, or None when it is absent or unreachable.
    """
    try:
        return _store().get(key)
    except _CACHE_ERRORS:
        logger.warning("%s could not be read from the cache", label, exc_info=True)
        return None


def get_many_or_empty(keys: list[str], *, label: str) -> dict[str, Any]:
    """Read several keys at once, treating a cache that cannot answer as all-missing.

    Args:
        keys: Cache keys.
        label: What is being read, for the log line when the cache is down.

    Returns:
        The entries that were present, or an empty mapping when the cache is unreachable.
    """
    try:
        return _store().get_many(keys)
    except _CACHE_ERRORS:
        logger.warning("%s could not be read from the cache", label, exc_info=True)
        return {}


def set_or_skip(key: str, value: Any, timeout: int, *, label: str) -> bool:
    """Store *value* under *key*, treating a cache that cannot accept it as a skip.

    Args:
        key: Cache key.
        value: What to store.
        timeout: Seconds to keep it.
        label: What is being stored, for the log line when it is refused.

    Returns:
        Whether it was stored.
    """
    try:
        _store().set(key, value, timeout)
    except _CACHE_ERRORS:
        logger.warning("%s could not be cached", label, exc_info=True)
        return False
    return True


def delete_quietly(key: str, *, label: str) -> None:
    """Drop *key*, tolerating a cache that cannot be reached.

    Args:
        key: Cache key.
        label: What is being dropped, for the log line when the cache is down.
    """
    try:
        _store().delete(key)
    except _CACHE_ERRORS:
        logger.warning("%s could not be dropped from the cache", label, exc_info=True)


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
    return set_or_skip(key, (content, content_type), timeout, label=label)
