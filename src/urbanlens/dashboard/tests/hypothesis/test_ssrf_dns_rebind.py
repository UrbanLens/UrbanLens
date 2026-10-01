"""A validated url is fetched at the address it validated to, not a re-resolution."""

from __future__ import annotations

import contextlib
import http.server
import ipaddress
import socket
import threading
from unittest import mock

import requests
import urllib3.util.connection

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.url_safety import (
    _PINS,
    UnsafeUrlError,
    _peer_address,
    _pin_key,
    _real_create_connection,
    _tracked_create_connection,
    fetch_public_url,
    is_blocked_address,
    resolve_public_http_url,
)

_URL_SAFETY = "urbanlens.dashboard.services.security.url_safety"


def _addrinfo(*ips: str) -> list[tuple]:
    """A getaddrinfo-shaped answer for ``ips``."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)) for ip in ips]


def _addrinfo_port(ip: str, port: int | str | None) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"x" * 512
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base class signature
        """Keep the test output clean."""


class ResolvePublicHttpUrlTests(SimpleTestCase):
    """The validator hands back the address it checked."""

    def test_it_returns_the_resolved_address(self) -> None:
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            url, ip = resolve_public_http_url("https://example.test/a.jpg")

        self.assertEqual(url, "https://example.test/a.jpg")
        self.assertEqual(ip, "93.184.216.34", "the caller cannot connect safely without the address that was validated")

    def test_a_literal_public_ip_is_its_own_address(self) -> None:
        _url, ip = resolve_public_http_url("https://93.184.216.34/a.jpg")

        self.assertEqual(ip, "93.184.216.34")

    def test_any_internal_answer_rejects_the_whole_url(self) -> None:
        """One bad address in a multi-answer response is enough to refuse it."""
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34", "127.0.0.1")),
            self.assertRaises(UnsafeUrlError),
        ):
            resolve_public_http_url("https://rebind.test/a.jpg")

    def test_internal_ranges_are_blocked(self) -> None:
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "::1"):
            with self.subTest(address=address):
                self.assertTrue(is_blocked_address(ipaddress.ip_address(address)))


class PinnedConnectionTests(SimpleTestCase):
    """The connection hook dials the pinned address, and is inert without a pin."""

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def _dial(self, host: str) -> tuple:
        with mock.patch(f"{_URL_SAFETY}._real_create_connection") as real:
            _tracked_create_connection((host, 443), 5)
        real.assert_called_once()
        return real.call_args.args[0]

    def test_without_a_pin_the_address_is_untouched(self) -> None:
        _PINS.map = None

        self.assertEqual(self._dial("example.test"), ("example.test", 443))

    def test_a_pinned_host_is_dialled_at_its_validated_address(self) -> None:
        """No hostname reaches urllib3's resolver, so nothing that answers for it can redirect the connection."""
        _PINS.map = {"rebind.test": "93.184.216.34"}

        self.assertEqual(self._dial("rebind.test"), ("93.184.216.34", 443))

    def test_a_pin_does_not_affect_other_hosts(self) -> None:
        _PINS.map = {"rebind.test": "93.184.216.34"}

        self.assertEqual(self._dial("other.test"), ("other.test", 443))

    def test_the_pin_matches_the_host_as_urllib3_spells_it(self) -> None:
        for fetched, dialled in (("bücher.test", "xn--bcher-kva.test"), ("Rebind.Test.", "rebind.test")):
            with self.subTest(fetched=fetched):
                _PINS.map = {_pin_key(fetched): "93.184.216.34"}

                self.assertEqual(self._dial(dialled), ("93.184.216.34", 443))


class PeerAddressTests(SimpleTestCase):
    """The backstop, exercised against a real socket rather than a double.

    This is deliberately not mocked."""

    def setUp(self) -> None:
        super().setUp()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def test_it_reads_the_peer_off_a_real_streamed_response(self) -> None:
        response = requests.get(f"http://127.0.0.1:{self.port}/", stream=True, allow_redirects=False, timeout=10)
        self.addCleanup(response.close)

        self.assertEqual(
            _peer_address(response),
            "127.0.0.1",
            "the peer must be readable before the body is consumed, or the check is decorative",
        )

    def test_a_real_connection_to_an_unvalidated_peer_is_refused(self) -> None:
        """End-to-end, with the pin disabled - the condition the backstop exists for.

        While the pin holds, the connection goes to the validated address by construction and this check can
        never fire."""
        url = f"http://127.0.0.1:{self.port}/"
        with (
            mock.patch(f"{_URL_SAFETY}._pinned_ip", return_value=None),
            mock.patch(f"{_URL_SAFETY}.resolve_public_http_url", return_value=(url, "93.184.216.34")),
            self.assertRaises(UnsafeUrlError),
        ):
            fetch_public_url(url)


class _CountingHandler(_Handler):
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        super().do_GET()


class PinSurvivesALaterResolverPatchTests(SimpleTestCase):
    """A resolver installed after url_safety's import (gevent, a DNS cache) must not route around the pin.

    The replacement rebinds: public for the validation, loopback for every later lookup."""

    def setUp(self) -> None:
        super().setUp()

        class Handler(_CountingHandler):
            hits = 0

        self.handler = Handler
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _rebinding_resolver():
        answered: list[str] = []

        def resolve(host, port, *_args, **_kwargs):
            try:
                return _addrinfo_port(str(ipaddress.ip_address(host)), port)
            except ValueError:
                pass
            ip = "93.184.216.34" if not answered else "127.0.0.1"
            answered.append(ip)
            return _addrinfo_port(ip, port)

        return resolve

    def _assert_fetches_never_reach_loopback(self, *later_patches: contextlib.AbstractContextManager) -> None:
        direct = requests.get(f"http://127.0.0.1:{self.port}/", timeout=5)
        self.assertEqual(direct.status_code, 200, "the loopback server must be reachable, or this test proves nothing")

        real_connect = socket.socket.connect
        connected_to: list[str] = []

        def recording_connect(sock, address):
            connected_to.append(address[0])
            return real_connect(sock, address)

        for host in ("rebind.test", "rebind.test.", "bücher.test"):
            with self.subTest(host=host):
                connected_to.clear()
                self.handler.hits = 0
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch("socket.getaddrinfo", self._rebinding_resolver()))
                    for later_patch in later_patches:
                        stack.enter_context(later_patch)
                    stack.enter_context(mock.patch.object(socket.socket, "connect", recording_connect))
                    with contextlib.suppress(Exception):
                        fetch_public_url(f"http://{host}:{self.port}/", timeout=2)

                self.assertEqual(self.handler.hits, 0, "the request reached a loopback server the validation refused")
                self.assertEqual(connected_to, ["93.184.216.34"], "the connection must go to the validated address")

    def test_a_later_getaddrinfo_patch_cannot_reach_loopback(self) -> None:
        self._assert_fetches_never_reach_loopback()

    def test_a_later_create_connection_patch_cannot_reach_loopback(self) -> None:
        """Reassigning urllib3's hook back to the stock function is put right before the next hop."""
        self._assert_fetches_never_reach_loopback(
            mock.patch("urllib3.util.connection.create_connection", _real_create_connection)
        )

    def test_the_hook_is_back_after_a_fetch(self) -> None:
        replacement = mock.Mock(side_effect=_real_create_connection)
        with (
            mock.patch("urllib3.util.connection.create_connection", replacement),
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            mock.patch("requests.get", return_value=mock.MagicMock(is_redirect=False)),
        ):
            fetch_public_url("https://example.test/")

            self.assertIs(urllib3.util.connection.create_connection, _tracked_create_connection)


class FetchPublicUrlTests(SimpleTestCase):
    """The fetch itself."""

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    @staticmethod
    def _response(*, status: int = 200, peer: str | None = "93.184.216.34", location: str | None = None):
        response = mock.MagicMock()
        response.status_code = status
        response.is_redirect = location is not None
        response.headers = {"Location": location} if location else {}
        response.raw._connection.sock.getpeername.return_value = (peer, 443) if peer else None
        if peer is None:
            response.raw._connection.sock = None
        return response

    def test_without_a_session_it_goes_through_requests_get(self) -> None:
        """The default path is ``requests.get``, which every caller's tests mock.

        Routing the default through a Session instead moves the seam out from under those mocks: the fetch stops
        being intercepted and starts making real connections."""
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            mock.patch("requests.get", return_value=self._response()) as get,
        ):
            fetch_public_url("https://example.test/a.jpg")

        get.assert_called_once()
        self.assertEqual(
            get.call_args.kwargs["allow_redirects"], False, "redirects are followed by hand so every hop is revalidated"
        )
        self.assertEqual(get.call_args.kwargs["stream"], True, "the body must not be read before the peer is checked")

    def test_a_response_without_a_socket_is_not_rejected(self) -> None:
        """ "Peer unknown" leaves the pin as the control; it must not fail the fetch.

        A cached response, a non-``requests`` adapter, or a test double exposes no socket."""

        class _NoRaw:
            status_code = 200
            is_redirect = False
            headers: dict[str, str] = {}

        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            mock.patch("requests.get", return_value=_NoRaw()),
        ):
            self.assertIsInstance(fetch_public_url("https://example.test/a.jpg"), _NoRaw)

    def test_the_pin_is_set_for_the_hostname_being_fetched(self) -> None:
        """The pin must be live at the moment requests resolves, not before or after."""
        seen: dict[str, str] = {}

        def capture(*_args, **_kwargs):
            seen.update(getattr(_PINS, "map", None) or {})
            return self._response()

        session = mock.MagicMock()
        session.get.side_effect = capture
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            fetch_public_url("https://example.test/a.jpg", session=session)

        self.assertEqual(seen, {"example.test": "93.184.216.34"})

    def test_the_pin_is_cleared_after_the_request(self) -> None:
        """A pin left behind would silently redirect an unrelated later fetch."""
        session = mock.MagicMock()
        session.get.return_value = self._response()
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            fetch_public_url("https://example.test/a.jpg", session=session)

        self.assertFalse(getattr(_PINS, "map", None))

    def test_a_connection_to_an_unvalidated_peer_is_refused(self) -> None:
        """The backstop: if the pin ever fails to apply, the body is never read."""
        session = mock.MagicMock()
        session.get.return_value = self._response(peer="127.0.0.1")
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(UnsafeUrlError),
        ):
            fetch_public_url("https://rebind.test/a.jpg", session=session)

        session.get.return_value.close.assert_called_once()

    def test_each_redirect_hop_is_validated_and_pinned(self) -> None:
        pins_per_hop: list[dict[str, str]] = []

        def capture(url, **_kwargs):
            pins_per_hop.append(dict(getattr(_PINS, "map", None) or {}))
            if "first.test" in url:
                return self._response(status=302, location="https://second.test/b.jpg")
            return self._response()

        session = mock.MagicMock()
        session.get.side_effect = capture
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            fetch_public_url("https://first.test/a.jpg", session=session)

        self.assertEqual(pins_per_hop, [{"first.test": "93.184.216.34"}, {"second.test": "93.184.216.34"}])

    def test_a_redirect_to_an_internal_host_is_refused(self) -> None:
        def resolve(host, *_args, **_kwargs):
            return _addrinfo("127.0.0.1") if host == "internal.test" else _addrinfo("93.184.216.34")

        session = mock.MagicMock()
        session.get.return_value = self._response(status=302, location="https://internal.test/b.jpg")
        with mock.patch("socket.getaddrinfo", side_effect=resolve), self.assertRaises(UnsafeUrlError):
            fetch_public_url("https://first.test/a.jpg", session=session)

    def test_a_redirect_chain_that_never_ends_is_refused(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = self._response(status=302, location="https://example.test/loop.jpg")
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(UnsafeUrlError),
        ):
            fetch_public_url("https://example.test/a.jpg", session=session, max_redirects=2)

        self.assertEqual(session.get.call_count, 3, "should try the original plus max_redirects hops, then give up")
