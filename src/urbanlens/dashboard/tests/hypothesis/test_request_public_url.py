"""The general outbound primitive: any method, host-limited redirects, byte caps and a real total deadline."""

from __future__ import annotations

import io
import socket
import time
from unittest import mock
import warnings

import requests
import urllib3.exceptions
import urllib3.util.connection

from urbanlens.core.tests.slow_servers import start_drip_server
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.url_safety import (
    _PINS,
    DeadlineExceededError,
    RedirectRefusedError,
    ResponseTooLargeError,
    UnsafeUrlError,
    _Deadline,
    _tracked_create_connection,
    open_public_url,
    request_public_url,
)

_RESOLVE = "urbanlens.dashboard.services.security.url_safety.resolve_public_http_url"


def _addrinfo(*ips: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)) for ip in ips]


def _response(
    *, status: int = 200, location: str | None = None, body: bytes = b"{}", headers: dict[str, str] | None = None
) -> mock.MagicMock:
    response = mock.MagicMock()
    response.status_code = status
    response.is_redirect = location is not None
    response.headers = {"Location": location} if location else dict(headers or {})
    response.iter_content.return_value = iter([body])
    response.raw._connection.sock = None
    return response


class SlowDripBodyTests(SimpleTestCase):
    """A server that keeps each read alive cannot keep the caller past the total deadline.

    Real sockets, because the property is about what a blocked ``recv`` does."""

    def test_a_body_dripped_inside_every_read_timeout_is_cut_at_the_deadline(self) -> None:
        url = start_drip_server(self)
        started = time.monotonic()
        with mock.patch(_RESOLVE, return_value=(url, "127.0.0.1")), self.assertRaises(DeadlineExceededError):
            request_public_url("GET", url, timeout=5, total_deadline=1)

        self.assertLess(time.monotonic() - started, 3, "the per-read timeout was the only bound")

    def test_reading_inside_an_open_block_is_cut_too(self) -> None:
        url = start_drip_server(self)
        started = time.monotonic()
        with (
            mock.patch(_RESOLVE, return_value=(url, "127.0.0.1")),
            self.assertRaises(DeadlineExceededError),
            open_public_url("GET", url, timeout=5, total_deadline=1) as response,
        ):
            for _chunk in response.iter_content(chunk_size=64):
                pass

        self.assertLess(time.monotonic() - started, 3)


class SlowDripHeaderTests(SimpleTestCase):
    """The header phase happens before any response exists to cut, so the connection hook covers it."""

    def test_headers_dripped_inside_every_read_timeout_are_cut_at_the_deadline(self) -> None:
        url = start_drip_server(self, drip_headers=True)
        started = time.monotonic()
        with mock.patch(_RESOLVE, return_value=(url, "127.0.0.1")), self.assertRaises(DeadlineExceededError):
            request_public_url("GET", url, timeout=5, total_deadline=1)

        self.assertLess(time.monotonic() - started, 3)

    def test_the_connection_hook_is_installed(self) -> None:
        self.assertIs(urllib3.util.connection.create_connection, _tracked_create_connection)


class SlowDripOverTlsTests(SimpleTestCase):
    """TLS wraps the socket the connection hook saw into a new object and detaches the original.

    A cut that only reached the original socket would do nothing over https."""

    def _unverified_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
        self.addCleanup(session.close)
        return session

    def _fetch(self, url: str) -> None:
        with (
            warnings.catch_warnings(),
            mock.patch(_RESOLVE, return_value=(url, "127.0.0.1")),
            self.assertRaises(DeadlineExceededError),
        ):
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            request_public_url("GET", url, session=self._unverified_session(), timeout=5, total_deadline=1)

    def test_https_headers_dripped_inside_every_read_timeout_are_cut_at_the_deadline(self) -> None:
        url = start_drip_server(self, drip_headers=True, tls=True)
        started = time.monotonic()
        self._fetch(url)

        self.assertLess(time.monotonic() - started, 3, "the header phase over TLS was bounded only per read")

    def test_an_https_body_dripped_inside_every_read_timeout_is_cut_at_the_deadline(self) -> None:
        url = start_drip_server(self, tls=True)
        started = time.monotonic()
        self._fetch(url)

        self.assertLess(time.monotonic() - started, 3)


class DeadlineBookkeepingTests(SimpleTestCase):
    """What the timer may and may not cut, on real socket pairs."""

    def _pair(self) -> tuple[socket.socket, socket.socket]:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def test_a_descriptor_is_cut_after_its_object_was_detached_and_rewrapped(self) -> None:
        """What TLS does to the socket urllib3 connected."""
        left, right = self._pair()
        deadline = _Deadline(60)
        deadline.track_socket(left)
        rewrapped = socket.socket(fileno=left.detach())
        self.addCleanup(rewrapped.close)

        deadline._expire()

        self.assertEqual(rewrapped.recv(1), b"", "the connection was not shut down")

    def test_a_forgotten_descriptor_is_never_cut(self) -> None:
        """After a hop closes its connection the number can belong to anything in the process."""
        left, right = self._pair()
        deadline = _Deadline(60)
        deadline.track_socket(left)
        deadline.forget()

        deadline._expire()

        right.sendall(b"x")
        self.assertEqual(left.recv(1), b"x")

    def test_nothing_is_cut_after_finish(self) -> None:
        left, right = self._pair()
        deadline = _Deadline(60)
        deadline.track_socket(left)
        deadline.finish()

        deadline._expire()

        right.sendall(b"x")
        self.assertEqual(left.recv(1), b"x")
        self.assertFalse(deadline.expired)


class MethodAndBodyTests(SimpleTestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_a_post_carries_its_body_without_following_redirects_itself(self) -> None:
        session = mock.MagicMock()
        session.post.return_value = _response()
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_public_url("POST", "https://push.test/up", session=session, json={"a": 1})

        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["json"], {"a": 1})
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["stream"], True)

    def test_without_a_session_it_uses_the_requests_module_function(self) -> None:
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            mock.patch("requests.post", return_value=_response()) as post,
        ):
            request_public_url("POST", "https://push.test/up", json={})

        post.assert_called_once()

    def test_the_body_is_served_from_memory(self) -> None:
        real = requests.Response()
        real.status_code = 200
        real.raw = io.BytesIO(b'{"ok": true}')
        session = mock.MagicMock()
        session.get.return_value = real
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            response = request_public_url("GET", "https://api.test/x", session=session)

        self.assertEqual(response.json(), {"ok": True})

    def test_a_307_repeats_the_post_and_a_302_turns_it_into_a_get(self) -> None:
        session = mock.MagicMock()
        session.post.side_effect = [_response(status=307, location="/again"), _response(status=302, location="/done")]
        session.get.return_value = _response()
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_public_url("POST", "https://api.test/x", session=session, json={"a": 1}, params={"q": "1"})

        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args_list[1].kwargs["json"], {"a": 1})
        self.assertNotIn("params", session.post.call_args_list[1].kwargs, "the query belongs to the first url only")
        self.assertNotIn("json", session.get.call_args.kwargs)

    def test_credential_headers_do_not_follow_a_redirect_to_another_host(self) -> None:
        session = mock.MagicMock()
        session.get.side_effect = [_response(status=302, location="https://elsewhere.test/"), _response()]
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_public_url(
                "GET",
                "https://api.test/x",
                session=session,
                headers={"x-api-key": "k", "Authorization": "t", "Accept": "a"},
                credential_headers=("x-api-key",),
            )

        self.assertEqual(session.get.call_args_list[1].kwargs["headers"], {"Accept": "a"})

    def test_credential_headers_survive_a_same_host_redirect(self) -> None:
        session = mock.MagicMock()
        session.get.side_effect = [_response(status=301, location="/api/"), _response()]
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_public_url(
                "GET",
                "https://api.test/api",
                session=session,
                headers={"x-api-key": "k"},
                credential_headers=("x-api-key",),
            )

        self.assertEqual(session.get.call_args_list[1].kwargs["headers"], {"x-api-key": "k"})


class RedirectPolicyTests(SimpleTestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_no_redirects_means_none_is_followed(self) -> None:
        session = mock.MagicMock()
        session.post.return_value = _response(status=307, location="https://push.test/elsewhere")
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(RedirectRefusedError),
        ):
            request_public_url("POST", "https://push.test/up", session=session, json={}, max_redirects=0)

        self.assertEqual(session.post.call_count, 1)

    def test_a_redirect_outside_the_allowed_hosts_is_refused_before_it_is_sent(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _response(status=302, location="https://evil.test/")
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(RedirectRefusedError),
        ):
            request_public_url(
                "GET", "https://web.archive.org/save/x", session=session, allowed_redirect_hosts=(".archive.org",)
            )

        self.assertEqual(session.get.call_count, 1)

    def test_a_subdomain_of_an_allowed_domain_is_followed(self) -> None:
        session = mock.MagicMock()
        session.get.side_effect = [_response(status=302, location="https://archive.org/done"), _response()]
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_public_url(
                "GET", "https://web.archive.org/save/x", session=session, allowed_redirect_hosts=(".archive.org",)
            )

        self.assertEqual(session.get.call_count, 2)

    def test_a_lookalike_domain_is_not_a_subdomain(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _response(status=302, location="https://notarchive.org/")
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(RedirectRefusedError),
        ):
            request_public_url(
                "GET", "https://web.archive.org/save/x", session=session, allowed_redirect_hosts=(".archive.org",)
            )

    def test_a_redirect_to_an_internal_address_is_refused(self) -> None:
        def resolve(host, *_args, **_kwargs):
            return _addrinfo("10.0.0.5") if host == "internal.test" else _addrinfo("93.184.216.34")

        session = mock.MagicMock()
        session.post.return_value = _response(status=307, location="https://internal.test/admin")
        with mock.patch("socket.getaddrinfo", side_effect=resolve), self.assertRaises(UnsafeUrlError):
            request_public_url("POST", "https://push.test/up", session=session, json={})

        self.assertEqual(session.post.call_count, 1)


class ByteCapTests(SimpleTestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_a_body_over_the_cap_is_refused(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _response(body=b"x" * 101)
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(ResponseTooLargeError),
        ):
            request_public_url("GET", "https://api.test/x", session=session, max_bytes=100)

    def test_a_declared_length_over_the_cap_is_refused_without_reading(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _response(headers={"Content-Length": "5000"})
        with (
            mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")),
            self.assertRaises(ResponseTooLargeError),
        ):
            request_public_url("GET", "https://api.test/x", session=session, max_bytes=100)

        session.get.return_value.iter_content.assert_not_called()

    def test_the_refusals_are_request_exceptions(self) -> None:
        """So every existing ``except requests.RequestException`` (and ``except OSError``) keeps catching them."""
        self.assertTrue(issubclass(ResponseTooLargeError, requests.RequestException))
        self.assertTrue(issubclass(DeadlineExceededError, requests.RequestException))
