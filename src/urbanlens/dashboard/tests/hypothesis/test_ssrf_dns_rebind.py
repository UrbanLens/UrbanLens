"""A validated url is fetched at the address it validated to, not a re-resolution.

``ensure_public_http_url`` used to return only the url string. Callers then
handed that string to ``requests``, which resolved the hostname a second time,
independently - so a host answering with a short TTL could return a public
address for the check and a loopback address for the connection. Re-validating
before every redirect hop made this worse rather than better: each hop is
another independent resolution the attacker gets to answer.

These tests pin the behaviour that closes it: resolve once, connect to *that*
address, and refuse the response if the socket turns out to be talking to
somewhere else.
"""

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
from unittest import mock

import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.url_safety import (
    _PINS,
    UnsafeUrlError,
    _peer_address,
    _pinned_getaddrinfo,
    _real_getaddrinfo,
    fetch_public_url,
    is_blocked_address,
    resolve_public_http_url,
)


def _addrinfo(*ips: str) -> list[tuple]:
    """A getaddrinfo-shaped answer for ``ips``."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)) for ip in ips]


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


class PinnedResolverTests(SimpleTestCase):
    """The resolver wrapper answers from the pin, and is inert without one."""

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_without_a_pin_it_delegates_to_the_real_resolver(self) -> None:
        _PINS.map = None
        with mock.patch(
            "urbanlens.dashboard.services.security.url_safety._real_getaddrinfo", return_value=_addrinfo("1.2.3.4")
        ) as real:
            result = _pinned_getaddrinfo("example.test", 443)

        real.assert_called_once()
        self.assertEqual(result[0][4][0], "1.2.3.4")

    def test_a_pinned_host_never_reaches_the_real_resolver(self) -> None:
        """This is the whole defence: no second resolution means nothing to poison."""
        _PINS.map = {"rebind.test": "93.184.216.34"}
        with mock.patch("urbanlens.dashboard.services.security.url_safety._real_getaddrinfo") as real:
            result = _pinned_getaddrinfo("rebind.test", 443)

        real.assert_not_called()
        self.assertEqual(result[0][4][0], "93.184.216.34")

    def test_a_pin_does_not_affect_other_hosts(self) -> None:
        _PINS.map = {"rebind.test": "93.184.216.34"}
        with mock.patch(
            "urbanlens.dashboard.services.security.url_safety._real_getaddrinfo", return_value=_addrinfo("5.6.7.8")
        ) as real:
            result = _pinned_getaddrinfo("other.test", 443)

        real.assert_called_once()
        self.assertEqual(result[0][4][0], "5.6.7.8")


class PeerAddressTests(SimpleTestCase):
    """The backstop, exercised against a real socket rather than a double.

    This is deliberately not mocked. ``_peer_address`` reads private urllib3
    attributes, so a double proves only that the double has the attribute the
    double was given: the check read ``raw._connection.sock``, which is
    ``None`` on urllib3 2.x for both http and https, and so silently returned
    ``None`` on every real response - the backstop never once fired, and no
    mock-based test could tell.
    """

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

        While the pin holds, the connection goes to the validated address by
        construction and this check can never fire. It matters only once the
        pin stops applying: the resolver wrapper is installed by assigning
        ``socket.getaddrinfo`` at import, so anything that reassigns it later
        (``gevent.monkey.patch_all`` under a preloading worker, another
        library, a stray patch) removes the pin silently. Restoring the real
        resolver here reproduces that, and the fetch must still refuse.
        """
        url = f"http://127.0.0.1:{self.port}/"
        with (
            mock.patch("socket.getaddrinfo", _real_getaddrinfo),
            mock.patch(
                "urbanlens.dashboard.services.security.url_safety.resolve_public_http_url",
                return_value=(url, "93.184.216.34"),
            ),
            self.assertRaises(UnsafeUrlError),
        ):
            fetch_public_url(url)


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

        Routing the default through a Session instead moves the seam out from
        under those mocks: the fetch stops being intercepted and starts making
        real connections. It also shares cookies across redirect hops, so one
        host's cookie is replayed to the next.
        """
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

        A cached response, a non-``requests`` adapter, or a test double exposes
        no socket. Reading through to ``.getpeername()`` unguarded raises
        mid-fetch instead.
        """

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
