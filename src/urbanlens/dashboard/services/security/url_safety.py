"""Shared SSRF guard for server-side requests to a host a user or a stored row chose."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import requests
import urllib3.util.connection

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping


class UnsafeUrlError(ValueError):
    """Raised when a url fails the public-reachability check."""


class RedirectRefusedError(UnsafeUrlError):
    """A response redirected further, or somewhere other, than the caller allowed."""


class ResponseTooLargeError(requests.RequestException):
    """The body ran past the caller's byte cap."""


class DeadlineExceededError(requests.Timeout):
    """The request, its redirects and its body together outlasted the caller's wall-clock budget."""


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
        ``(validated_url, ip_address)`` - connect to ``ip_address``, not to a re-resolution of the hostname.

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
# applying, silently. gevent's monkey-patching is one such reassignment.
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


#: A ``requests`` timeout: one number for every phase, or ``(connect, read)``.
type Timeout = float | tuple[float, float]

#: What :func:`request_public_url` reads into memory when the caller names no ceiling.
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

#: Wall-clock budget for one request, its redirects and its body, when the caller names none.
DEFAULT_TOTAL_DEADLINE_SECONDS = 60.0

_READ_CHUNK_BYTES = 64 * 1024

_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

#: Dropped whenever a redirect leaves the host they were sent to, as a browser would.
_ALWAYS_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

#: The deadline of the request this thread is sending, read by the connection hook below.
_DEADLINES = threading.local()


def _shutdown_fd(fd: int) -> None:
    """Shut down the connection on *fd* without closing the descriptor, whichever object owns it."""
    with contextlib.suppress(OSError):
        view = socket.socket(fileno=fd)
        try:
            view.shutdown(socket.SHUT_RDWR)
        finally:
            view.detach()


class _Deadline:
    """One request's wall-clock budget, and the connections to cut when it runs out.

    A read timeout bounds each ``recv``, not their sum, so a server that sends a byte just inside it
    holds the caller indefinitely. A timer shuts the request's sockets down from outside, which ends
    whatever read is blocked on them.
    """

    def __init__(self, seconds: float) -> None:
        self.expires_at = time.monotonic() + seconds
        self.expired = False
        self._finished = False
        self._fds: list[int] = []
        self._shutdowns: list[Any] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def start(self) -> None:
        """Start the clock."""
        self._timer.start()

    def remaining(self) -> float:
        """Seconds left, negative once past."""
        return self.expires_at - time.monotonic()

    def track_socket(self, sock: socket.socket) -> None:
        """Cut *sock*'s connection at expiry; a new connection is registered here as it opens.

        The descriptor rather than the object, because TLS wraps the socket into a new object and
        detaches this one.

        Args:
            sock: The freshly connected socket.
        """
        fd = sock.fileno()
        with self._lock:
            if not self.expired:
                self._fds.append(fd)
                return
        _shutdown_fd(fd)

    def forget(self) -> None:
        """Drop everything tracked so far, before the hop that opened it closes its connection.

        A closed descriptor can be reused by anything in the process, so nothing may cut it later.
        """
        with self._lock:
            self._fds.clear()
            self._shutdowns.clear()

    def track_response(self, response: requests.Response) -> None:
        """Cut *response*'s connection at expiry, covering one reused from a pool.

        Args:
            response: A streamed response whose body has not been read.
        """
        shutdown = getattr(getattr(response, "raw", None), "shutdown", None)
        if shutdown is None:
            return
        with self._lock:
            if not self.expired:
                self._shutdowns.append(shutdown)
                return
        self._call_shutdown(shutdown)

    @staticmethod
    def _call_shutdown(shutdown: Any) -> None:
        # urllib3 refuses once the connection is back in the pool, which means the read is over.
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            shutdown()

    def _expire(self) -> None:
        # The cuts stay under the lock so finish() cannot return mid-cut: once it does, the caller
        # closes these descriptors and their numbers can be reused by another thread.
        with self._lock:
            if self._finished:
                return
            self.expired = True
            for fd in self._fds:
                _shutdown_fd(fd)
            for shutdown in self._shutdowns:
                self._call_shutdown(shutdown)

    def finish(self) -> None:
        """Stop the clock; waits out a cut already in progress, and nothing is cut after this returns."""
        with self._lock:
            self._finished = True
        self._timer.cancel()

    def hop_timeout(self, timeout: Timeout) -> Timeout:
        """*timeout*, shortened to what is left of the budget.

        Args:
            timeout: The caller's per-phase timeout.

        Returns:
            The timeout for the next hop.

        Raises:
            DeadlineExceededError: Nothing is left.
        """
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceededError("The request ran out of time before its next hop.")
        if isinstance(timeout, tuple):
            return (min(timeout[0], remaining), min(timeout[1], remaining))
        return min(timeout, remaining)


_real_create_connection = urllib3.util.connection.create_connection


def _tracked_create_connection(*args: Any, **kwargs: Any) -> socket.socket:
    """urllib3's ``create_connection``, registering the socket with this thread's deadline, if any.

    The only point where a new connection's socket exists before its TLS handshake and response
    headers are read, both of which a slow server can drip out as slowly as a body.
    """
    sock = _real_create_connection(*args, **kwargs)
    deadline = getattr(_DEADLINES, "current", None)
    if deadline is not None:
        deadline.track_socket(sock)
    return sock


# Same install-once, thread-local-gated shape as the resolver pin above, with the same caveat:
# anything that later reassigns this attribute silently disables the header-phase deadline.
if urllib3.util.connection.create_connection is not _tracked_create_connection:  # pragma: no branch - idempotent install
    urllib3.util.connection.create_connection = _tracked_create_connection


def _host_allowed(hostname: str, allowed: Collection[str]) -> bool:
    """Whether a redirect to *hostname* stays inside *allowed*.

    Args:
        hostname: The redirect target's host.
        allowed: Exact hostnames, or ``.example.org`` for that domain and every subdomain of it.

    Returns:
        True when some entry matches.
    """
    host = hostname.lower().rstrip(".")
    for raw_entry in allowed:
        entry = raw_entry.lower()
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _retire(response: requests.Response, deadline: _Deadline | None) -> None:
    """Close a hop's response, untracking its connection first so the deadline cannot cut a reused descriptor."""
    if deadline is not None:
        deadline.forget()
    response.close()


def _send(
    method: str,
    url: str,
    *,
    session: requests.Session | None,
    params: Mapping[str, Any] | None,
    json: Any,
    data: Any,
    headers: Mapping[str, str] | None,
    timeout: Timeout,
    max_redirects: int,
    max_length: int,
    allowed_redirect_hosts: Collection[str] | None,
    credential_headers: Collection[str],
    deadline: _Deadline | None,
) -> requests.Response:
    """Send one request, following redirects by hand so every hop is resolved, pinned and checked.

    Returns:
        The final streamed, non-redirect response.

    Raises:
        UnsafeUrlError: A hop failed validation, a redirect had no target, or the connection landed on an address that was not the validated one.
        RedirectRefusedError: A redirect left ``allowed_redirect_hosts`` or the chain exceeded ``max_redirects``.
        DeadlineExceededError: *deadline* ran out between hops.
        requests.RequestException: The underlying request failed.
    """
    method = method.upper()
    if method not in _METHODS:
        raise ValueError(f"Unsupported method {method!r}")
    # The module functions rather than `session.request`, so the `requests.get`/`requests.post` seam
    # every caller's tests patch keeps intercepting.
    sender: Any = session if session is not None else requests
    stripped = _ALWAYS_CREDENTIAL_HEADERS | {name.lower() for name in credential_headers}
    send_headers = dict(headers or {})
    body = {key: value for key, value in (("json", json), ("data", data)) if value is not None}
    query = params
    fetch_url = url
    for _hop in range(max_redirects + 1):
        fetch_url, ip = resolve_public_http_url(fetch_url, max_length=max_length)
        parts = urlsplit(fetch_url)
        hostname = parts.hostname or ""
        kwargs: dict[str, Any] = {
            "headers": send_headers,
            "timeout": deadline.hop_timeout(timeout) if deadline is not None else timeout,
            "stream": True,
            "allow_redirects": False,
            **body,
        }
        if query is not None:
            kwargs["params"] = query

        previous = getattr(_PINS, "map", None)
        _PINS.map = {hostname: ip}
        try:
            response = getattr(sender, method.lower())(fetch_url, **kwargs)
        finally:
            _PINS.map = previous
        if deadline is not None:
            deadline.track_response(response)

        peer = _peer_address(response)
        if peer is not None and peer != ip:
            _retire(response, deadline)
            raise UnsafeUrlError("That link can't be processed.")

        # is_permanent_redirect is a strict subset of is_redirect in requests
        # (301/308 vs 301/302/303/307/308), so this covers every redirect.
        if not response.is_redirect:
            return response

        target = response.headers.get("Location")
        status = response.status_code
        _retire(response, deadline)
        if not target:
            raise UnsafeUrlError("That link can't be processed.")
        next_url = urljoin(fetch_url, target)
        next_parts = urlsplit(next_url)
        next_host = (next_parts.hostname or "").lower()
        if allowed_redirect_hosts is not None and not _host_allowed(next_host, allowed_redirect_hosts):
            raise RedirectRefusedError(f"A redirect from {hostname} to {next_host} left the allowed hosts.")
        if next_host != hostname.lower() or (parts.scheme == "https" and next_parts.scheme != "https"):
            send_headers = {name: value for name, value in send_headers.items() if name.lower() not in stripped}
        # requests' own rewrite (`rebuild_method`): only 307/308 repeat the method and body.
        if (status in (302, 303) and method != "HEAD") or (status == 301 and method == "POST"):
            method, body = "GET", {}
        query = None
        fetch_url = next_url
    raise RedirectRefusedError(f"More than {max_redirects} redirects from {urlsplit(url).hostname}.")


def _close_connection(response: requests.Response) -> None:
    """Close *response* and the connection under it, rather than returning that to the pool.

    ``Response.close`` leaves a fully read connection open for reuse, and a reused connection's
    header phase happens before any deadline has a response to cut.
    """
    close_raw = getattr(getattr(response, "raw", None), "close", None)
    if close_raw is not None:
        with contextlib.suppress(OSError):
            close_raw()
    response.close()


def read_limited(response: requests.Response, *, max_bytes: int) -> bytes:
    """Read a streamed body, refusing one longer than *max_bytes*.

    Args:
        response: A response opened with ``stream=True``, as every one here is.
        max_bytes: Largest body to accept.

    Returns:
        The body.

    Raises:
        ResponseTooLargeError: The declared length or the bytes actually sent exceed *max_bytes*.
    """
    declared = str(response.headers.get("Content-Length", "")).strip()
    if declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(f"The response declared {declared} bytes, over the {max_bytes}-byte limit.")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"The response exceeded the {max_bytes}-byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@contextlib.contextmanager
def open_public_url(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: Timeout = 20,
    total_deadline: float = DEFAULT_TOTAL_DEADLINE_SECONDS,
    max_redirects: int = 5,
    allowed_redirect_hosts: Collection[str] | None = None,
    credential_headers: Collection[str] = (),
    max_length: int = 2048,
) -> Iterator[requests.Response]:
    """Open a request to a host a user or a stored row chose, bounded as a whole by *total_deadline*.

    Every hop is resolved, checked against internal ranges and connected to the address that was
    checked; redirects are followed by hand, within ``allowed_redirect_hosts``. The deadline covers
    everything inside the ``with`` block, the body included, and the connection is closed on exit.

    Args:
        method: HTTP method.
        url: Where to send it.
        session: Send on this session (e.g. a ``Gateway``'s rate-limited, logging one).
        params: Query parameters, sent to the first hop only.
        json: JSON body. Dropped when a redirect turns the request into a GET.
        data: Form or raw body, likewise.
        headers: Request headers.
        timeout: Per-phase timeout for each hop, shortened to what is left of the deadline.
        total_deadline: Seconds the whole exchange may take, redirects and body included.
        max_redirects: Redirects to follow; 0 refuses every redirect.
        allowed_redirect_hosts: Hosts a redirect may lead to (``.example.org`` for a domain and its
            subdomains), or None for any public host.
        credential_headers: Header names carrying a credential, dropped when a redirect changes host;
            ``Authorization``, ``Cookie`` and ``Proxy-Authorization`` always are.
        max_length: Reject urls longer than this.

    Yields:
        The final streamed, non-redirect response.

    Raises:
        UnsafeUrlError: A hop failed validation or connected somewhere other than the address checked.
        RedirectRefusedError: A redirect went further or elsewhere than allowed.
        DeadlineExceededError: The exchange outlasted *total_deadline*, including while the block read the body.
        requests.RequestException: The underlying request failed.
    """
    host = urlsplit(url).hostname
    deadline = _Deadline(total_deadline)
    deadline.start()
    response: requests.Response | None = None
    try:
        previous = getattr(_DEADLINES, "current", None)
        _DEADLINES.current = deadline
        try:
            response = _send(
                method,
                url,
                session=session,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=timeout,
                max_redirects=max_redirects,
                max_length=max_length,
                allowed_redirect_hosts=allowed_redirect_hosts,
                credential_headers=credential_headers,
                deadline=deadline,
            )
        finally:
            _DEADLINES.current = previous
        yield response
    except Exception as exc:
        if deadline.expired and not isinstance(exc, DeadlineExceededError):
            raise DeadlineExceededError(f"A request to {host} outlasted its {total_deadline}s deadline.") from exc
        raise
    finally:
        deadline.finish()
        if response is not None:
            _close_connection(response)
    # A cut socket can read as a clean end of body, so a block that finished is not proof of a whole one.
    if deadline.expired:
        raise DeadlineExceededError(f"A request to {host} outlasted its {total_deadline}s deadline.")


def request_public_url(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: Timeout = 20,
    total_deadline: float = DEFAULT_TOTAL_DEADLINE_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_redirects: int = 5,
    allowed_redirect_hosts: Collection[str] | None = None,
    credential_headers: Collection[str] = (),
    max_length: int = 2048,
) -> requests.Response:
    """:func:`open_public_url`, with the body read under *max_bytes* before the deadline ends.

    Args:
        method: HTTP method.
        url: Where to send it.
        session: As :func:`open_public_url`.
        params: As :func:`open_public_url`.
        json: As :func:`open_public_url`.
        data: As :func:`open_public_url`.
        headers: As :func:`open_public_url`.
        timeout: As :func:`open_public_url`.
        total_deadline: As :func:`open_public_url`.
        max_bytes: Largest body to read.
        max_redirects: As :func:`open_public_url`.
        allowed_redirect_hosts: As :func:`open_public_url`.
        credential_headers: As :func:`open_public_url`.
        max_length: As :func:`open_public_url`.

    Returns:
        The final response, its connection closed and ``content``/``json()`` served from memory.

    Raises:
        UnsafeUrlError: As :func:`open_public_url`.
        RedirectRefusedError: As :func:`open_public_url`.
        DeadlineExceededError: As :func:`open_public_url`.
        ResponseTooLargeError: The body is longer than *max_bytes*.
        requests.RequestException: The underlying request failed.
    """
    with open_public_url(
        method,
        url,
        session=session,
        params=params,
        json=json,
        data=data,
        headers=headers,
        timeout=timeout,
        total_deadline=total_deadline,
        max_redirects=max_redirects,
        allowed_redirect_hosts=allowed_redirect_hosts,
        credential_headers=credential_headers,
        max_length=max_length,
    ) as response:
        response._content = b"" if method.upper() == "HEAD" else read_limited(response, max_bytes=max_bytes)  # noqa: SLF001 - how requests itself holds a read body
    return response


def fetch_public_url(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20,
    max_redirects: int = 5,
    max_length: int = 2048,
    session: requests.Session | None = None,
) -> requests.Response:
    """GET ``url`` through the same hop loop as :func:`open_public_url`, leaving the body to the caller.

    There is no overall deadline here, because the caller reads the body after this returns; a
    caller that needs one uses :func:`open_public_url`.

    Args:
        url: The url to fetch.
        headers: Extra request headers (e.g. a descriptive User-Agent).
        timeout: Per-request timeout, in seconds.
        max_redirects: Redirect hops to follow before giving up.
        max_length: Reject urls longer than this.
        session: Issue the requests on this session (e.g. a ``Gateway``'s rate-limited, logging session).

    Returns:
        The final streamed, non-redirect ``requests.Response``.

    Raises:
        UnsafeUrlError: A hop failed validation, a redirect had no target, the connection landed on an address that was not the validated one, or the chain exceeded ``max_redirects``.
        requests.RequestException: The underlying request failed.
    """
    return _send(
        "GET",
        url,
        session=session,
        params=None,
        json=None,
        data=None,
        headers=headers,
        timeout=timeout,
        max_redirects=max_redirects,
        max_length=max_length,
        allowed_redirect_hosts=None,
        credential_headers=(),
        deadline=None,
    )
