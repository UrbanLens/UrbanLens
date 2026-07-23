"""Shared SSRF guard for server-side fetches of a user-supplied url.

Used anywhere the app downloads content from a url a user (not a fixed,
trusted provider) supplied - each such fetch runs from inside the server's
own network, so an unvalidated url lets a user direct outbound requests at
internal services (SSRF), including cloud metadata endpoints.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """Raised when a url fails the public-reachability check."""


def is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``address`` shouldn't be reachable from a user-directed fetch."""
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast


def ensure_public_http_url(url: str, *, max_length: int = 2048) -> str:
    """Validate ``url`` is http(s) and doesn't target a loopback/private/internal host.

    Checks both literal IPs in the url and, by resolving the hostname, any
    domain that currently points at one. This closes the DNS-at-check-time
    gap but not a rebind that happens *between* this check and an eventual
    connection - callers making the actual request should re-validate
    immediately before each connection attempt (including every redirect
    hop) to keep that window as small as possible; see
    ``services.ai.link_extraction.fetch_page_text`` for the pattern.

    Args:
        url: The url to validate.
        max_length: Reject anything longer than this.

    Returns:
        The validated url, unchanged.

    Raises:
        UnsafeUrlError: On any rejection, with a user-facing message.
    """
    url = (url or "").strip()
    if not url or len(url) > max_length:
        raise UnsafeUrlError("That link isn't usable.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrlError("Only http(s) links can be processed.")
    hostname = parts.hostname
    if hostname == "localhost":
        raise UnsafeUrlError("That link can't be processed.")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if is_blocked_address(literal_address):
            raise UnsafeUrlError("That link can't be processed.")
        return url

    try:
        resolved = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise UnsafeUrlError("That link can't be processed.") from exc
    for _family, _type, _proto, _canonname, sockaddr in resolved:
        if is_blocked_address(ipaddress.ip_address(sockaddr[0])):
            raise UnsafeUrlError("That link can't be processed.")
    return url
