"""Tracking parameters, which a site adds to a URL without changing what it points at."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_TRACKING_PREFIX = "utm_"


def without_tracking_params(url: str) -> str:
    """*url* with its ``utm_*`` query parameters removed, everything else kept as written.

    Wikimedia began adding them to Commons and Wikipedia file URLs in 2026-09, and only sometimes, so the same file
    arrived under two URLs.

    Args:
        url: Any URL, or any string.

    Returns:
        The URL without tracking parameters; *url* itself when it has none.
    """
    if _TRACKING_PREFIX not in url.casefold():
        return url
    parts = urlsplit(url)
    pairs = parts.query.split("&")
    kept = [pair for pair in pairs if not pair.casefold().startswith(_TRACKING_PREFIX)]
    if len(kept) == len(pairs):
        return url
    return urlunsplit(parts._replace(query="&".join(kept)))
