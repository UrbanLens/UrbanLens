"""Network guards for test runs.

The application has several integrations that normally talk to third-party
services. Unit tests should mock those integrations explicitly. This module
provides a process-wide socket guard so accidental external network calls fail
fast while still allowing localhost services such as the test database, Redis,
or an in-process test server.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

_LOCALHOST_NAMES = {"", "localhost", "localhost.localdomain"}


def _host_is_localhost(host: Any) -> bool:
    """Return True when ``host`` points at the local machine."""
    if host is None:
        return True

    if isinstance(host, bytes):
        host = host.decode("idna", errors="ignore")

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
        return (
            "External network access is disabled during tests. "
            f"Attempted to connect to {host!r}; mock this integration or use localhost."
        )

    def _guarded_connect(self, sock: socket.socket, address: Any) -> Any:
        host = _address_host(address)
        if not _host_is_localhost(host):
            raise RuntimeError(self._blocked_message(host))
        return self._original_connect(sock, address)

    def _guarded_create_connection(
        self,
        address: tuple[Any, int],
        timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
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

    def start(self) -> "LocalhostOnlyNetwork":
        def guarded_connect(sock: socket.socket, address: Any) -> Any:
            return self._guarded_connect(sock, address)

        self._stack.enter_context(patch("socket.create_connection", self._guarded_create_connection))
        self._stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        return self

    def stop(self) -> None:
        self._stack.close()
