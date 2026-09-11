"""Memcached-safe Django cache key helpers."""

from __future__ import annotations

import hashlib


def make_cache_key(namespace: str, *parts: str | float) -> str:
    """Build a memcached-safe cache key from a namespace and parts.

    Args:
        namespace: Short identifier for the cache entry type.
        *parts: Values that distinguish entries within the namespace.

    Returns:
        A cache key safe for all Django cache backends.
    """
    if not parts:
        return namespace
    # Length-prefix parts so ("a:b",) and ("a", "b") hash differently.
    raw = "".join(f"{len(encoded := str(part))}:{encoded}" for part in parts)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{namespace}:{digest}"
