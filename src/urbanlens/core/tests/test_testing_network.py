"""Tests for the localhost-only network guard used during test runs."""

from __future__ import annotations

import socket
from unittest import mock

from urbanlens.core.testing_network import (
    VERIFY_PROBE_ADDRESS,
    VERIFY_PROBE_HOSTNAME,
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    _address_host,
    _host_is_localhost,
    _host_needs_no_lookup,
    database_hosts,
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

    def test_hostname_containing_localhost_substring_is_not_localhost(self) -> None:
        # Guards against a substring/prefix/suffix check standing in for exact
        # matching, which would let an attacker-controlled hostname like
        # "localhost.evil.com" slip through the guard as if it were local.
        for name in ("localhost.evil.com", "notlocalhost", "evil-localhost.com"):
            with self.subTest(name=name):
                self.assertFalse(_host_is_localhost(name))

    def test_private_non_loopback_ip_is_not_localhost(self) -> None:
        # RFC1918/link-local/unique-local ranges are private but routable over a
        # real network - conflating `is_loopback` with `is_private` would let the
        # guard wave through connections to another machine on the LAN or a
        # Docker bridge network, not just this process.
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.5", "169.254.1.1", "fc00::1", "fe80::1"):
            with self.subTest(host=host):
                self.assertFalse(_host_is_localhost(host))


class HostNeedsNoLookupTests(TestCase):
    """``_host_needs_no_lookup``: a name the resolver must be asked about is the only thing refused."""

    def test_local_names_and_missing_hosts_need_none(self) -> None:
        for host in (None, "", "localhost", b"LOCALHOST", "localhost."):
            with self.subTest(host=host):
                self.assertTrue(_host_needs_no_lookup(host))

    def test_ip_literals_need_none_whether_or_not_they_are_loopback(self) -> None:
        """Resolving a literal asks nobody. A routable one is still stopped, at ``connect``."""
        for host in ("127.0.0.1", "::1", "93.184.216.34", "10.0.0.1", "2606:4700::1111", "fe80::1%eth0", b"8.8.8.8"):
            with self.subTest(host=host):
                self.assertTrue(_host_needs_no_lookup(host))

    def test_any_other_name_needs_one(self) -> None:
        for host in (
            "example.com",
            "api.example.test",
            "db",
            "test_db",
            "localhost.evil.com",
            "redis.internal",
            "1.2.3",
        ):
            with self.subTest(host=host):
                self.assertFalse(_host_needs_no_lookup(host))


class DatabaseHostsTests(TestCase):
    def test_names_come_from_every_configured_database(self) -> None:
        databases = {"default": {"HOST": "urbanlens_db"}, "replica": {"HOST": "Replica.Internal."}}

        self.assertEqual(database_hosts(databases), {"urbanlens_db", "replica.internal"})

    def test_a_host_list_is_split(self) -> None:
        self.assertEqual(database_hosts({"default": {"HOST": "db-a, db-b"}}), {"db-a", "db-b"})

    def test_blank_missing_and_socket_directory_hosts_are_not_names(self) -> None:
        databases = {"a": {"HOST": ""}, "b": {}, "c": {"HOST": None}, "d": {"HOST": "/var/run/postgresql"}}

        self.assertEqual(database_hosts(databases), frozenset())


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

    def test_blocks_external_connect_ex(self) -> None:
        """`connect_ex` is a separate C-level method - it does not route through `connect`.

        A guard that patches only `connect` reports success here and opens a real outbound socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                sock.connect_ex(("1.1.1.1", 443))
            self.assertIn("External network access is disabled during tests", str(ctx.exception))
        finally:
            sock.close()

    def test_allows_localhost_connect_ex(self) -> None:
        """Anti-vacuity: the guard must not break `connect_ex` against loopback."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _host, port = server.getsockname()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            self.assertEqual(client.connect_ex(("127.0.0.1", port)), 0)
        finally:
            client.close()
            server.close()

    def test_blocks_external_udp_sendto(self) -> None:
        """A datagram needs no connect at all, so `connect`/`create_connection` never see it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                sock.sendto(b"probe", ("1.1.1.1", 53))
            self.assertIn("External network access is disabled during tests", str(ctx.exception))
        finally:
            sock.close()

    def test_allows_localhost_udp_sendto(self) -> None:
        """Anti-vacuity: loopback datagrams still work."""
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        _host, port = server.getsockname()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.assertEqual(client.sendto(b"probe", ("127.0.0.1", port)), 5)
        finally:
            client.close()
            server.close()

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

    def test_blocks_external_name_lookup(self) -> None:
        """``connect`` is never reached for a name: the resolver is asked first, and that query leaves the machine."""
        lookups = {
            "getaddrinfo": lambda: socket.getaddrinfo("example.com", 443),
            "getaddrinfo by keyword": lambda: socket.getaddrinfo(host="example.com", port=443),
            "getaddrinfo bytes": lambda: socket.getaddrinfo(b"example.com", 443),
            "gethostbyname": lambda: socket.gethostbyname("example.com"),
            "gethostbyname_ex": lambda: socket.gethostbyname_ex("example.com"),
        }
        for name, lookup in lookups.items():
            with self.subTest(name), self.assertRaises(RuntimeError) as ctx:
                lookup()
            self.assertIn("External network access is disabled during tests", str(ctx.exception))
            self.assertIn("example.com", str(ctx.exception))

    def test_blocks_name_lookup_through_a_client_library(self) -> None:
        """requests resolves through urllib3's own ``getaddrinfo`` call, before any ``connect``."""
        import requests

        with self.assertRaises(RuntimeError) as ctx:
            requests.get("http://dns-leak-probe.example/", timeout=1)

        self.assertIn("External network access is disabled during tests", str(ctx.exception))

    def test_allows_localhost_lookups(self) -> None:
        """Anti-vacuity: the guard must not break the lookups a loopback server needs."""
        self.assertTrue(socket.getaddrinfo("localhost", 80))
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))
        self.assertTrue(socket.getaddrinfo(None, 80, flags=socket.AI_PASSIVE))
        self.assertEqual(socket.gethostbyname("127.0.0.1"), "127.0.0.1")
        self.assertTrue(socket.gethostbyname("localhost").startswith("127."))

    def test_allows_an_ip_literal_that_is_not_loopback(self) -> None:
        """A literal resolves without asking anyone, and the database host under ``host_pytest.sh`` is one (the runner's bridge address)."""
        self.assertEqual(socket.getaddrinfo("93.184.216.34", 443, type=socket.SOCK_STREAM)[0][4][0], "93.184.216.34")
        self.assertEqual(socket.gethostbyname("93.184.216.34"), "93.184.216.34")

    def test_a_configured_database_host_may_be_looked_up_and_nothing_else_may(self) -> None:
        """psycopg resolves a database's service name in Python; that name is the deployment's own."""
        guard = LocalhostOnlyNetwork(lookup_hosts=["urbanlens_db"])
        guard._original_getaddrinfo = mock.Mock(return_value=["answer"])  # noqa: SLF001 - keeps the lookup off the resolver
        guard.start()
        self.addCleanup(guard.stop)

        self.assertEqual(socket.getaddrinfo("URBANLENS_DB.", 5432), ["answer"])
        with self.assertRaises(RuntimeError):
            socket.getaddrinfo("example.com", 443)

    def test_a_patched_lookup_replaces_the_guard_and_is_not_refused_first(self) -> None:
        """Tests that stub ``socket.getaddrinfo`` for the SSRF checks keep working."""
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=answer):
            self.assertEqual(socket.getaddrinfo("example.com", 443), answer)

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
        with (
            mock.patch(
                "urbanlens.core.testing_network.socket.create_connection",
                side_effect=RuntimeError(
                    "External network access is disabled during tests. "
                    "Attempted to connect to '1.1.1.1'; mock this integration or use localhost."
                ),
            ),
            mock.patch(
                "urbanlens.core.testing_network.socket.getaddrinfo",
                side_effect=RuntimeError(
                    "External network access is disabled during tests. "
                    f"Attempted to resolve {VERIFY_PROBE_HOSTNAME!r}; mock this integration or use localhost."
                ),
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

    def test_fails_when_a_name_lookup_reaches_the_resolver(self) -> None:
        refusal = RuntimeError(
            "External network access is disabled during tests. Attempted to connect to '1.1.1.1'; mock this integration or use localhost."
        )
        with (
            mock.patch("urbanlens.core.testing_network.socket.create_connection", side_effect=refusal),
            mock.patch(
                "urbanlens.core.testing_network.socket.getaddrinfo", side_effect=socket.gaierror("no such host")
            ),
            self.assertRaises(ExternalNetworkGuardVerificationError) as ctx,
        ):
            verify_external_network_blocked()

        self.assertIn(VERIFY_PROBE_HOSTNAME, str(ctx.exception))
        self.assertIn("resolver", str(ctx.exception))

    def test_fails_when_a_name_lookup_succeeds(self) -> None:
        refusal = RuntimeError(
            "External network access is disabled during tests. Attempted to connect to '1.1.1.1'; mock this integration or use localhost."
        )
        with (
            mock.patch("urbanlens.core.testing_network.socket.create_connection", side_effect=refusal),
            mock.patch("urbanlens.core.testing_network.socket.getaddrinfo", return_value=[]),
            self.assertRaises(ExternalNetworkGuardVerificationError) as ctx,
        ):
            verify_external_network_blocked()

        self.assertIn("succeeded", str(ctx.exception))

    def test_uses_default_probe_address(self) -> None:
        with (
            mock.patch(
                "urbanlens.core.testing_network.socket.create_connection",
                side_effect=RuntimeError(
                    "External network access is disabled during tests. "
                    f"Attempted to connect to {VERIFY_PROBE_ADDRESS[0]!r}; "
                    "mock this integration or use localhost."
                ),
            ) as create_connection,
            mock.patch(
                "urbanlens.core.testing_network.socket.getaddrinfo",
                side_effect=RuntimeError("External network access is disabled during tests."),
            ),
        ):
            verify_external_network_blocked()

        create_connection.assert_called_once_with(VERIFY_PROBE_ADDRESS, timeout=0.5)
