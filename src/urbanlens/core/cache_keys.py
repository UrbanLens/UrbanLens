"""Memcached-safe Django cache key helpers."""

from __future__ import annotations

import hashlib
import string


def make_cache_key(namespace: str, *parts: str | float) -> str:
    """Build a memcached-safe cache key from a namespace and variable parts.

    Memcached keys must not contain spaces or control characters. Variable
    user-supplied values are hashed so keys remain safe regardless of content.

    Args:
        namespace: Short identifier for the cache entry type (e.g. ``smithsonian``).
        *parts: Values that distinguish entries within the namespace.

    Returns:
        A cache key safe for all Django cache backends, including memcached.
    """
    if not parts:
        return namespace
    raw = ":".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{namespace}:{digest}"


def is_memcached_safe_key(key: str) -> bool:
    """Return True if ``key`` contains only memcached-safe characters.

    Args:
        key: The cache key to validate.

    Returns:
        True when the key has no spaces or control characters.
    """
    safe = set(string.ascii_letters + string.digits + "_-.:")
    return all(char in safe for char in key)
