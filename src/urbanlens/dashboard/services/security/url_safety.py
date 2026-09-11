"""Shared SSRF guard for server-side fetches of a user-supplied url.
Used anywhere the app downloads content from a url a user (not a fixed, trusted provider) supplied - each such fetch runs from inside the server's own network, so an unvalidated url lets a user direct outbound requests at internal services (SSRF), including cloud metadata endpoints."""

from __future__ import annotations

import ipaddress
import socket
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

    import requests


class UnsafeUrlError(ValueError):
    """Raised when a url fails the public-reachability check."""


#: RFC 6598 Carrier-Grade-NAT / Shared-Address-Space range.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``address`` shouldn't be reachable from a user-directed fetch."""
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_NETWORK:
        return True
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast


def resolve_public_http_url(url: str, *, max_length: int = 2048) -> tuple[str, str]:
    """Validate ``url`` and return it alongside the address it resolved to.
    Returning the address is the point: a caller that gets only the url back has no way to connect to the host it was told is safe, because the next resolution is a fresh, unvalidated one.

    Args:
        url: The url to validate.
        max_length: Reject anything longer than this.

    Returns:
        ``(validated_url, ip_address)`` - connect to ``ip_address``, not to a
        re-resolution of the hostname.

    Raises:
        UnsafeUrlError: On any rejection, with a user-facing message."""
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
        return url, str(literal_address)

    try:
        resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeUrlError("That link can't be processed.") from exc
    if not resolved:
        raise UnsafeUrlError("That link can't be processed.")
    # Every answer must be public, and the one we hand back is the one we
    # connect to. Checking all of them then connecting to a re-resolution
    # would let an attacker return a good set now and a bad one later.
    addresses = [ipaddress.ip_address(sockaddr[0]) for *_head, sockaddr in resolved]
    for address in addresses:
        if is_blocked_address(address):
            raise UnsafeUrlError("That link can't be processed.")
    return url, str(addresses[0])


def ensure_public_http_url(url: str, *, max_length: int = 2048) -> str:
    """Validate ``url`` is http(s) and doesn't currently resolve to an internal host.
    This remains for *submission-time* validation, where the point is to reject an obviously-internal link at the moment a user pastes it rather than to protect a fetch: the value it returns is only a url, so a caller that passes it to ``requests`` re-resolves and reopens the rebind window.

    Args:
        url: The url to validate.
        max_length: Reject anything longer than this.

    Returns:
        The validated url, unchanged.

    Raises:
        UnsafeUrlError: On any rejection, with a user-facing message."""
    return resolve_public_http_url(url, max_length=max_length)[0]


#: Per-thread ``{hostname: ip}`` pins consulted by the resolver wrapper below.
#: Thread-local so a pin installed for one fetch cannot affect a concurrent
#: request, and so the wrapper is a no-op for every caller that isn't fetching.
_PINS = threading.local()

_real_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    """``socket.getaddrinfo`` that answers from the active pin when one exists.
    Installed once, process-wide, but gated on a thread-local: with no pin set it delegates straight to the real resolver, so ordinary DNS is untouched."""
    pins = getattr(_PINS, "map", None)
    if pins and host in pins:
        ip = pins[host]
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]
    return _real_getaddrinfo(host, port, *args, **kwargs)


# Installed by assignment at import, which makes ordering matter: anything that reassigns
# socket.getaddrinfo *after* this module is imported replaces the wrapper and the pin stops
# applying, silently.
# The live case is gevent - gunicorn runs `-k gevent` and monkey-patches the socket module in the
if socket.getaddrinfo is not _pinned_getaddrinfo:  # pragma: no branch - idempotent install
    socket.getaddrinfo = _pinned_getaddrinfo


#: Where the live socket hangs off a streamed ``requests`` response, most current first.
#: These are private attributes, so they move between urllib3 releases - ``_connection.sock`` is the
#: documented-looking one and is ``None`` on urllib3 2.x for both http and https, which is why more
#: than one is tried and why a test asserts against a real socket rather than a double.
_SOCKET_PATHS = (
    ("_fp", "fp", "raw", "_sock"),
    ("_connection", "sock"),
)


def _peer_address(response: requests.Response) -> str | None:
    """The IP the response's socket is actually connected to, if determinable.
    Every step is optional-by-construction, and the result is returned only if it actually parses as an IP address."""
    raw = getattr(response, "raw", None)
    if raw is None:
        return None
    for path in _SOCKET_PATHS:
        sock = raw
        for attr in path:
            sock = getattr(sock, attr, None)
            if sock is None:
                break
        if sock is None:
            continue
        try:
            peer = sock.getpeername()[0]
            return str(ipaddress.ip_address(peer))
        except (OSError, AttributeError, LookupError, TypeError, ValueError):
            continue
    return None


def fetch_public_url(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20,
    max_redirects: int = 5,
    max_length: int = 2048,
    session: requests.Session | None = None,
) -> requests.Response:
    """Fetch ``url`` with SSRF protection that survives a DNS rebind.
    Redirects are followed manually so every hop gets the same treatment.

    Args:
        url: The url to fetch.
        headers: Extra request headers (e.g. a descriptive User-Agent).
        timeout: Per-request timeout, in seconds.
        max_redirects: Redirect hops to follow before giving up.
        max_length: Reject urls longer than this.
        session: Issue the requests on this session (e.g. a ``Gateway``'s
            rate-limited, logging session). When omitted each hop goes through
            ``requests.get``, which uses a fresh session per hop - so a cookie
            set by one host in a redirect chain is never replayed to the next.

    Returns:
        The final streamed, non-redirect ``requests.Response``. Callers must
        still bound how much of the body they read.

    Raises:
        UnsafeUrlError: A hop failed validation, a redirect had no target, the
            connection landed on an address that was not the validated one, or
            the chain exceeded ``max_redirects``.
        requests.RequestException: The underlying request failed."""
    import requests as _requests

    get = _requests.get if session is None else session.get
    fetch_url = url
    for _hop in range(max_redirects + 1):
        fetch_url, ip = resolve_public_http_url(fetch_url, max_length=max_length)
        hostname = urlsplit(fetch_url).hostname or ""

        previous = getattr(_PINS, "map", None)
        _PINS.map = {hostname: ip}
        try:
            response = get(
                fetch_url,
                headers=dict(headers or {}),
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        finally:
            _PINS.map = previous

        peer = _peer_address(response)
        if peer is not None and peer != ip:
            response.close()
            raise UnsafeUrlError("That link can't be processed.")

        # is_permanent_redirect is a strict subset of is_redirect in requests
        # (301/308 vs 301/302/303/307/308), so this covers every redirect.
        if not response.is_redirect:
            return response

        target = response.headers.get("Location")
        response.close()
        if not target:
            raise UnsafeUrlError("That link can't be processed.")
        fetch_url = _requests.compat.urljoin(fetch_url, target)
    raise UnsafeUrlError("That link can't be processed.")
