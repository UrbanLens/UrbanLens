"""Fail fast on accidental external network calls and name lookups in tests; localhost still allowed."""

from __future__ import annotations

from contextlib import ExitStack
import ipaddress
import os
import socket
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_LOCALHOST_NAMES = {"", "localhost", "localhost.localdomain"}

# Start of every refusal this guard raises; callers and verification match on it.
_BLOCKED_MARKER = "External network access is disabled during tests"

# Probe used to confirm the guard blocks outbound sockets.
VERIFY_PROBE_ADDRESS: tuple[str, int] = ("1.1.1.1", 443)

# Probe used to confirm the guard blocks name lookups. ``.invalid`` never resolves (RFC 6761), so a guard that is
# missing costs one query that finds nothing.
VERIFY_PROBE_HOSTNAME = "dns-guard-probe.invalid"


class ExternalNetworkGuardVerificationError(RuntimeError):
    """Guard inactive or misconfigured."""


def _host_is_localhost(host: Any) -> bool:
    """Return True when host points at the local machine."""
    if host is None:
        return True

    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")

    host = str(host).strip().lower().rstrip(".")
    if host in _LOCALHOST_NAMES:
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_needs_no_lookup(host: Any) -> bool:
    """Return True when resolving ``host`` asks no resolver anything.

    That is no host, a local name, or an IP literal. A literal resolves to itself, so letting it through leaks
    nothing; connecting to a routable one is still refused by the connect guards. Anything else is a name, and
    resolving it sends a query off the machine before any connect is attempted.

    Args:
        host: What was passed to a lookup function.

    Returns:
        True when the lookup is answered without the network.
    """
    if _host_is_localhost(host):
        return True

    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")

    # A scoped IPv6 literal ("fe80::1%eth0") names an interface, not a host.
    literal = str(host).strip().partition("%")[0]
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return False
    return True


def _normalized_name(host: Any) -> str:
    """Lower-case, strip and un-dot a host as given to a lookup, so names compare the way DNS does."""
    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")
    return str(host).strip().lower().rstrip(".")


def database_hosts(databases: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Names of the database servers a ``DATABASES`` setting points at.

    psycopg resolves a server's name in Python before libpq connects, so under the guard a suite whose database is
    reached by a service name (a devcontainer's ``urbanlens_db``) could not open its connection. The name is the
    deployment's own, not somewhere a test wandered off to.

    Args:
        databases: Django's ``DATABASES`` mapping.

    Returns:
        The normalized host names; socket directories and blanks are not names.
    """
    names: set[str] = set()
    for config in databases.values():
        # libpq takes a comma-separated list of hosts.
        for part in str(config.get("HOST") or "").split(","):
            name = _normalized_name(part)
            if name and not name.startswith("/"):
                names.add(name)
    return frozenset(names)


def _configured_database_hosts() -> frozenset[str]:
    """The database hosts of the settings in use, or of ``UL_DB_HOST`` when Django has none loaded yet."""
    from django.conf import settings

    if settings.configured:
        return database_hosts(settings.DATABASES)
    return database_hosts({"default": {"HOST": os.environ.get("UL_DB_HOST")}})


def _address_host(address: Any) -> Any:
    """Extract the host from a socket address."""
    if isinstance(address, tuple) and address:
        return address[0]
    return None


class LocalhostOnlyNetwork:
    """Deny non-localhost socket destinations and name lookups.

    Args:
        lookup_hosts: Names whose lookup is allowed although they are not local. Defaults to the configured
            database servers (see :func:`database_hosts`).
    """

    def __init__(self, lookup_hosts: Iterable[str] | None = None) -> None:
        self._lookup_hosts = frozenset(_normalized_name(host) for host in lookup_hosts) if lookup_hosts is not None else _configured_database_hosts()
        self._stack = ExitStack()
        self._original_connect = socket.socket.connect
        self._original_connect_ex = socket.socket.connect_ex
        self._original_sendto = socket.socket.sendto
        self._original_create_connection = socket.create_connection
        self._original_getaddrinfo = socket.getaddrinfo
        self._original_gethostbyname = socket.gethostbyname
        self._original_gethostbyname_ex = socket.gethostbyname_ex

    def _blocked_message(self, host: Any) -> str:
        return f"{_BLOCKED_MARKER}. Attempted to connect to {host!r}; mock this integration or use localhost."

    def _guard_lookup(self, host: Any) -> None:
        if not _host_needs_no_lookup(host) and _normalized_name(host) not in self._lookup_hosts:
            raise RuntimeError(f"{_BLOCKED_MARKER}. Attempted to resolve {host!r}; mock this integration or use localhost.")

    def _guarded_getaddrinfo(self, *args: Any, **kwargs: Any) -> Any:
        self._guard_lookup(args[0] if args else kwargs.get("host"))
        return self._original_getaddrinfo(*args, **kwargs)

    def _guarded_gethostbyname(self, hostname: Any, /) -> Any:
        self._guard_lookup(hostname)
        return self._original_gethostbyname(hostname)

    def _guarded_gethostbyname_ex(self, hostname: Any, /) -> Any:
        self._guard_lookup(hostname)
        return self._original_gethostbyname_ex(hostname)

    def _guarded_connect(self, sock: socket.socket, address: Any) -> Any:
        host = _address_host(address)
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_connect(sock, address)

    def _guarded_connect_ex(self, sock: socket.socket, address: Any) -> Any:
        host = _address_host(address)
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_connect_ex(sock, address)

    def _guarded_sendto(self, sock: socket.socket, *args: Any) -> Any:
        # Address is always last.
        host = _address_host(args[-1]) if args else None
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_sendto(sock, *args)

    def _guarded_create_connection(
        self,
        address: tuple[Any, int],
        timeout: float | None = None,
        source_address: tuple[Any, int] | None = None,
        all_errors: bool = False,
    ) -> socket.socket:
        host = _address_host(address)
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_create_connection(
            address,
            timeout=timeout,
            source_address=source_address,
            all_errors=all_errors,
        )

    def start(self) -> LocalhostOnlyNetwork:
        def guarded_connect(sock: socket.socket, address: Any) -> Any:
            return self._guarded_connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
            return self._guarded_connect_ex(sock, address)

        def guarded_sendto(sock: socket.socket, *args: Any) -> Any:
            return self._guarded_sendto(sock, *args)

        self._stack.enter_context(patch("socket.create_connection", self._guarded_create_connection))
        self._stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        # connect_ex and sendto bypass connect(), so patch them separately.
        self._stack.enter_context(patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        self._stack.enter_context(patch.object(socket.socket, "sendto", guarded_sendto))
        # Resolving a name sends a query before any connect, so it is guarded in its own right. gethostbyaddr is not:
        # it takes an address, and Django's mail module resolves the machine's own name through it.
        self._stack.enter_context(patch("socket.getaddrinfo", self._guarded_getaddrinfo))
        self._stack.enter_context(patch("socket.gethostbyname", self._guarded_gethostbyname))
        self._stack.enter_context(patch("socket.gethostbyname_ex", self._guarded_gethostbyname_ex))
        return self

    def stop(self) -> None:
        self._stack.close()


def verify_external_network_blocked(
    probe_address: tuple[str, int] = VERIFY_PROBE_ADDRESS,
    probe_hostname: str = VERIFY_PROBE_HOSTNAME,
) -> None:
    """Confirm outbound non-localhost connections and name lookups are blocked.

    Args:
        probe_address: Host/port pair that must be rejected.
        probe_hostname: Name whose lookup must be rejected.

    Raises:
        ExternalNetworkGuardVerificationError: When verification fails.
    """
    host, _port = probe_address
    try:
        connection = socket.create_connection(probe_address, timeout=0.5)
    except RuntimeError as exc:
        if _BLOCKED_MARKER not in str(exc):
            raise ExternalNetworkGuardVerificationError(
                f"Network guard verification failed: unexpected RuntimeError while probing {host!r}: {exc}",
            ) from exc
    except OSError as exc:
        raise ExternalNetworkGuardVerificationError(
            f"Network guard verification failed: connection to external host {host!r} reached the OS network stack instead of being blocked by LocalhostOnlyNetwork ({exc}).",
        ) from exc
    else:
        connection.close()
        raise ExternalNetworkGuardVerificationError(
            f"Network guard verification failed: connection to external host {host!r} succeeded while tests require blocked external access.",
        )

    try:
        socket.getaddrinfo(probe_hostname, 443)
    except RuntimeError as exc:
        if _BLOCKED_MARKER not in str(exc):
            raise ExternalNetworkGuardVerificationError(
                f"Network guard verification failed: unexpected RuntimeError while resolving {probe_hostname!r}: {exc}",
            ) from exc
    except OSError as exc:
        raise ExternalNetworkGuardVerificationError(
            f"Network guard verification failed: a lookup of {probe_hostname!r} reached the resolver instead of being blocked by LocalhostOnlyNetwork ({exc}).",
        ) from exc
    else:
        raise ExternalNetworkGuardVerificationError(
            f"Network guard verification failed: a lookup of {probe_hostname!r} succeeded while tests require blocked external access.",
        )
