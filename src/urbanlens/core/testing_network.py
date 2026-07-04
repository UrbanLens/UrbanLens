"""Network guards for test runs.

The application has several integrations that normally talk to third-party
services. Unit tests should mock those integrations explicitly. This module
provides a process-wide socket guard so accidental external network calls fail
fast while still allowing localhost services such as the test database, Redis,
or an in-process test server.
"""

from __future__ import annotations

from contextlib import ExitStack
import ipaddress
import socket
from typing import Any
from unittest.mock import patch

_LOCALHOST_NAMES = {"", "localhost", "localhost.localdomain"}

# Public probe used to confirm the guard blocks outbound sockets before tests run.
VERIFY_PROBE_ADDRESS: tuple[str, int] = ("1.1.1.1", 443)


class ExternalNetworkGuardVerificationError(RuntimeError):
    """Raised when the localhost-only network guard is inactive or misconfigured."""


def _host_is_localhost(host: Any) -> bool:
    """Return True when ``host`` points at the local machine."""
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


def _address_host(address: Any) -> Any:
    """Extract the host from a socket address, if it has one."""
    if isinstance(address, tuple) and address:
        return address[0]
    return None


class LocalhostOnlyNetwork:
    """Patch socket connection APIs to deny non-localhost destinations."""

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._original_connect = socket.socket.connect
        self._original_create_connection = socket.create_connection

    def _blocked_message(self, host: Any) -> str:
        return f"External network access is disabled during tests. Attempted to connect to {host!r}; mock this integration or use localhost."

    def _guarded_connect(self, sock: socket.socket, address: Any) -> Any:
        host = _address_host(address)
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_connect(sock, address)

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

        self._stack.enter_context(patch("socket.create_connection", self._guarded_create_connection))
        self._stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        return self

    def stop(self) -> None:
        self._stack.close()


def verify_external_network_blocked(
    probe_address: tuple[str, int] = VERIFY_PROBE_ADDRESS,
) -> None:
    """Confirm outbound connections to non-localhost hosts are blocked.

    Call this after ``LocalhostOnlyNetwork.start()`` during test bootstrap.
    Exits the process when the guard is missing or allows external traffic.

    Args:
        probe_address: Host/port pair that must be rejected by the guard.

    Raises:
        ExternalNetworkGuardVerificationError: When verification fails.
    """
    host, _port = probe_address
    try:
        connection = socket.create_connection(probe_address, timeout=0.5)
    except RuntimeError as exc:
        if "External network access is disabled during tests" in str(exc):
            return
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
