"""Outbound calls to provider hosts still go through the same guard as user-chosen ones.

Every caller today builds these urls on a fixed host - Google's avatar CDN, Discord, Gravatar,
web.archive.org - but the fetch itself must not trust that: a redirect, or a future caller passing
something else, would otherwise reach internal addresses.
"""

from __future__ import annotations

import io
import socket
from unittest import mock

import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.wayback_machine import WaybackMachineGateway
from urbanlens.dashboard.services.notifications.notifications import _send_gotify
from urbanlens.dashboard.services.profile.avatar import AvatarService
from urbanlens.dashboard.services.security.url_safety import _PINS


def _resolving(default: str = "93.184.216.34", **hosts: str):
    def answer(host, *_args, **_kwargs):
        ip = hosts.get(host.replace(".", "_").replace("-", "_"), default)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    return mock.patch("socket.getaddrinfo", side_effect=answer)


def _body(content: bytes, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.raw = io.BytesIO(content)
    return response


def _redirect(location: str) -> mock.MagicMock:
    response = mock.MagicMock(status_code=302, is_redirect=True, headers={"Location": location})
    response.raw._connection.sock = None
    return response


class AvatarDownloadTests(SimpleTestCase):
    GET = "requests.get"

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_a_url_resolving_internally_is_not_fetched(self) -> None:
        with _resolving(default="10.0.0.1"), mock.patch(self.GET) as get:
            self.assertIsNone(AvatarService.download("https://cdn.example.test/a.png"))

        get.assert_not_called()

    def test_redirects_are_followed_by_hand_and_an_internal_one_is_refused(self) -> None:
        with (
            _resolving(metadata_internal="169.254.169.254"),
            mock.patch(self.GET, return_value=_redirect("http://metadata.internal/")) as get,
        ):
            self.assertIsNone(AvatarService.download("https://cdn.example.test/a.png"))

        self.assertEqual(get.call_count, 1)
        self.assertIs(get.call_args.kwargs.get("allow_redirects"), False)

    def test_an_oversized_avatar_is_refused(self) -> None:
        with _resolving(), mock.patch(self.GET, return_value=_body(b"x" * (512 * 1024 + 1))):
            self.assertIsNone(AvatarService.download("https://cdn.example.test/a.png"))

    def test_an_ordinary_avatar_downloads(self) -> None:
        with _resolving(), mock.patch(self.GET, return_value=_body(b"png-bytes")):
            self.assertEqual(AvatarService.download("https://cdn.example.test/a.png"), b"png-bytes")

    def test_a_non_http_scheme_is_refused(self) -> None:
        with mock.patch(self.GET) as get:
            self.assertIsNone(AvatarService.download("file:///etc/passwd"))

        get.assert_not_called()


class WaybackSaveTests(SimpleTestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_a_redirect_off_archive_org_is_not_followed(self) -> None:
        """The Archive fetches the user's url, not us; a redirect elsewhere would make us fetch it."""
        session = mock.MagicMock()
        session.get.return_value = _redirect("https://internal.example.test/")
        gateway = WaybackMachineGateway(session=session)
        with _resolving(internal_example_test="10.0.0.9"), self.assertRaises(requests.RequestException):
            gateway.save_url("https://example.org/page")

        self.assertEqual(session.get.call_count, 1)

    def test_a_redirect_to_a_public_host_off_archive_org_is_not_followed_either(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _redirect("https://example.org/page")
        gateway = WaybackMachineGateway(session=session)
        with _resolving(), self.assertRaises(requests.RequestException):
            gateway.save_url("https://example.org/page")

        self.assertEqual(session.get.call_count, 1)

    def test_a_redirect_within_archive_org_reaches_the_snapshot(self) -> None:
        snapshot = _body(b"<html></html>")
        snapshot.url = "https://web.archive.org/web/20260924/https://example.org/page"
        session = mock.MagicMock()
        session.get.side_effect = [_redirect(snapshot.url), snapshot]
        gateway = WaybackMachineGateway(session=session)
        with _resolving():
            saved = gateway.save_url("https://example.org/page")

        self.assertEqual(saved, {"archived_url": snapshot.url, "status_code": 200})


class _Site:
    notify_gotify_url = "https://gotify.example.test"
    notify_gotify_token = "secret-token"  # noqa: S105 - a stub attribute named like the real field


class GotifyTokenTests(SimpleTestCase):
    def test_the_token_travels_in_a_header_and_never_in_the_url(self) -> None:
        """Proxies and access logs record the query string; they do not record this header."""
        with mock.patch("requests.post", return_value=mock.Mock(ok=True, status_code=200)) as post:
            _send_gotify(_Site(), "subject", "message")

        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Gotify-Key"], "secret-token")
        self.assertNotIn("secret-token", str(post.call_args.args) + str(kwargs.get("params")))
