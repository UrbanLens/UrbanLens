"""Tests for the localhost-only network guard used during test runs."""

from __future__ import annotations

import socket
from unittest import mock

from urbanlens.core.testing_network import (
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    VERIFY_PROBE_ADDRESS,
    _address_host,
    _host_is_localhost,
    verify_external_network_blocked,
)
from urbanlens.core.tests.testcase import TestCase


class HostIsLocalhostTests(TestCase):
    """``_host_is_localhost`` recognises loopback destinations."""

    def test_none_is_localhost(self) -> None:
        self.assertTrue(_host_is_localhost(None))

    def test_empty_string_is_localhost(self) -> None:
        self.assertTrue(_host_is_localhost(""))

    def test_localhost_names_are_localhost(self) -> None:
        for name in ("localhost", "localhost.localdomain", "LOCALHOST"):
            with self.subTest(name=name):
                self.assertTrue(_host_is_localhost(name))

    def test_loopback_ipv4_is_localhost(self) -> None:
        self.assertTrue(_host_is_localhost("127.0.0.1"))
        self.assertTrue(_host_is_localhost("127.255.255.254"))

    def test_loopback_ipv6_is_localhost(self) -> None:
        self.assertTrue(_host_is_localhost("::1"))

    def test_bytes_localhost_is_localhost(self) -> None:
        self.assertTrue(_host_is_localhost(b"localhost"))

    def test_external_ipv4_is_not_localhost(self) -> None:
        self.assertFalse(_host_is_localhost("8.8.8.8"))
        self.assertFalse(_host_is_localhost("1.1.1.1"))

    def test_external_hostname_is_not_localhost(self) -> None:
        self.assertFalse(_host_is_localhost("example.com"))


class AddressHostTests(TestCase):
    """``_address_host`` extracts hosts from socket address tuples."""

    def test_tuple_address_returns_host(self) -> None:
        self.assertEqual(_address_host(("127.0.0.1", 8080)), "127.0.0.1")

    def test_empty_tuple_returns_none(self) -> None:
        self.assertIsNone(_address_host(()))

    def test_non_tuple_returns_none(self) -> None:
        self.assertIsNone(_address_host("127.0.0.1"))


class LocalhostOnlyNetworkTests(TestCase):
    """``LocalhostOnlyNetwork`` blocks non-localhost socket connections."""

    def test_blocks_external_create_connection(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            socket.create_connection(("8.8.8.8", 53), timeout=0.1)

        self.assertIn("External network access is disabled during tests", str(ctx.exception))
        self.assertIn("8.8.8.8", str(ctx.exception))

    def test_blocks_external_socket_connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                sock.connect(("1.1.1.1", 443))
            self.assertIn("External network access is disabled during tests", str(ctx.exception))
        finally:
            sock.close()

    def test_allows_localhost_connection(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _host, port = server.getsockname()
        client: socket.socket | None = None
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=1)
        finally:
            if client is not None:
                client.close()
            server.close()

    def test_nested_guard_stop_leaves_session_guard_active(self) -> None:
        guard = LocalhostOnlyNetwork().start()
        guard.stop()

        with self.assertRaises(RuntimeError) as ctx:
            socket.create_connection(("8.8.8.8", 53), timeout=0.1)

        self.assertIn("External network access is disabled during tests", str(ctx.exception))

    def test_start_returns_self(self) -> None:
        guard = LocalhostOnlyNetwork()
        self.assertIs(guard.start(), guard)

    def test_stop_is_idempotent(self) -> None:
        guard = LocalhostOnlyNetwork().start()
        guard.stop()
        guard.stop()


class VerifyExternalNetworkBlockedTests(TestCase):
    """``verify_external_network_blocked`` validates the active guard."""

    def test_passes_when_guard_blocks_probe(self) -> None:
        with mock.patch(
            "urbanlens.core.testing_network.socket.create_connection",
            side_effect=RuntimeError(
                "External network access is disabled during tests. "
                "Attempted to connect to '1.1.1.1'; mock this integration or use localhost."
            ),
        ):
            verify_external_network_blocked()

    def test_fails_when_connection_succeeds(self) -> None:
        connection = mock.Mock()
        with (
            mock.patch(
                "urbanlens.core.testing_network.socket.create_connection",
                return_value=connection,
            ),
            self.assertRaises(ExternalNetworkGuardVerificationError) as ctx,
        ):
            verify_external_network_blocked()

        self.assertIn("succeeded", str(ctx.exception))
        connection.close.assert_called_once_with()

    def test_fails_when_os_error_reaches_network_stack(self) -> None:
        with (
            mock.patch(
                "urbanlens.core.testing_network.socket.create_connection",
                side_effect=TimeoutError("timed out"),
            ),
            self.assertRaises(ExternalNetworkGuardVerificationError) as ctx,
        ):
            verify_external_network_blocked()

        self.assertIn("reached the OS network stack", str(ctx.exception))

    def test_fails_on_unexpected_runtime_error(self) -> None:
        with (
            mock.patch(
                "urbanlens.core.testing_network.socket.create_connection",
                side_effect=RuntimeError("different failure"),
            ),
            self.assertRaises(ExternalNetworkGuardVerificationError) as ctx,
        ):
            verify_external_network_blocked()

        self.assertIn("unexpected RuntimeError", str(ctx.exception))

    def test_uses_default_probe_address(self) -> None:
        with mock.patch(
            "urbanlens.core.testing_network.socket.create_connection",
            side_effect=RuntimeError(
                "External network access is disabled during tests. "
                f"Attempted to connect to {VERIFY_PROBE_ADDRESS[0]!r}; "
                "mock this integration or use localhost."
            ),
        ) as create_connection:
            verify_external_network_blocked()

        create_connection.assert_called_once_with(VERIFY_PROBE_ADDRESS, timeout=0.5)
