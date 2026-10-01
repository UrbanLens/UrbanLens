"""The Immich gateway talks to a server the account holder chose, so every request is an SSRF door.

The form checks the url when it is saved. Nothing checked it when it was used: the host could
re-resolve to an internal address, redirect there with the API key attached, drip bytes forever, or
answer a JSON call with a body of any size - and the thumbnail bytes go back to the browser.
"""

from __future__ import annotations

import io
import socket
import time
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker
import requests

from urbanlens.core.tests.slow_servers import start_drip_server
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.services.apis.immich.gateway import GatewayRequestError, ImmichGateway
from urbanlens.dashboard.services.security.url_safety import _PINS

SERVER = "https://photos.example.com"


def _account(server_url: str = SERVER) -> ImmichAccount:
    return ImmichAccount(profile=baker.make(User).profile, server_url=server_url, api_key="test-key")


def _resolving(**hosts: str):
    """Answer DNS for each host with its address, and a public address for anything else."""

    def answer(host, *_args, **_kwargs):
        ip = hosts.get(host.replace(".", "_"), "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    return mock.patch("socket.getaddrinfo", side_effect=answer)


def _redirect(location: str) -> mock.MagicMock:
    response = mock.MagicMock(status_code=302, is_redirect=True, headers={"Location": location})
    response.raw._connection.sock = None
    return response


def _json_response(body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.raw = io.BytesIO(body)
    return response


class ImmichRedirectTests(TestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_a_redirect_to_an_internal_address_is_not_followed(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _redirect("http://metadata.internal/latest/")
        gateway = ImmichGateway(account=_account(), session=session)
        with _resolving(metadata_internal="169.254.169.254"), self.assertRaises(GatewayRequestError):
            gateway.get_map_markers()

        self.assertEqual(session.get.call_count, 1)
        self.assertIs(session.get.call_args.kwargs.get("allow_redirects"), False)

    def test_a_redirect_to_another_public_host_is_not_followed_with_the_api_key(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _redirect("https://collector.example.net/")
        gateway = ImmichGateway(account=_account(), session=session)
        with _resolving(), self.assertRaises(GatewayRequestError):
            gateway.get_map_markers()

        self.assertEqual(session.get.call_count, 1)

    def test_a_thumbnail_redirect_to_an_internal_address_is_not_followed(self) -> None:
        session = mock.MagicMock()
        session.get.return_value = _redirect("http://10.0.0.5/secret.png")
        gateway = ImmichGateway(account=_account(), session=session)
        with _resolving(), self.assertRaises(GatewayRequestError):
            gateway.get_asset_thumbnail("asset-1")

        self.assertEqual(session.get.call_count, 1)


class ImmichRebindingTests(TestCase):
    """Public when the form saved it, internal when it is used."""

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def _gateway(self) -> ImmichGateway:
        return ImmichGateway(account=_account(), session=mock.MagicMock())

    def test_a_json_get_to_a_host_now_resolving_internally_is_not_sent(self) -> None:
        gateway = self._gateway()
        with _resolving(photos_example_com="10.0.0.5"), self.assertRaises(GatewayRequestError):
            gateway.get_map_markers()

        gateway.session.get.assert_not_called()

    def test_a_json_post_to_a_host_now_resolving_internally_is_not_sent(self) -> None:
        gateway = self._gateway()
        with _resolving(photos_example_com="127.0.0.1"), self.assertRaises(GatewayRequestError):
            gateway.list_recent()

        gateway.session.post.assert_not_called()

    def test_a_thumbnail_from_a_host_now_resolving_internally_is_not_fetched(self) -> None:
        gateway = self._gateway()
        with _resolving(photos_example_com="192.168.1.10"), self.assertRaises(GatewayRequestError):
            gateway.get_asset_thumbnail("asset-1")

        gateway.session.get.assert_not_called()

    def test_the_request_connects_to_the_address_that_was_checked(self) -> None:
        pins: list[dict[str, str]] = []

        def capture(*_args, **_kwargs):
            pins.append(dict(getattr(_PINS, "map", None) or {}))
            return _json_response(b"[]")

        gateway = self._gateway()
        gateway.session.get.side_effect = capture
        with _resolving(photos_example_com="93.184.216.40"):
            gateway.get_map_markers()

        self.assertEqual(pins, [{"photos.example.com": "93.184.216.40"}])


class ImmichJsonCapTests(TestCase):
    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_the_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, "IMMICH_MAX_JSON_BYTES"))

    @override_settings(IMMICH_MAX_JSON_BYTES=1024)
    def test_an_oversized_json_body_is_refused_rather_than_parsed(self) -> None:
        gateway = ImmichGateway(account=_account(), session=mock.MagicMock())
        gateway.session.get.return_value = _json_response(b"[" + b" " * 2048 + b"]")
        with _resolving(), self.assertRaises(GatewayRequestError):
            gateway.get_map_markers()

    @override_settings(IMMICH_MAX_JSON_BYTES=1024)
    def test_a_body_under_the_ceiling_still_parses(self) -> None:
        gateway = ImmichGateway(account=_account(), session=mock.MagicMock())
        gateway.session.get.return_value = _json_response(b'[{"id": "a", "lat": 1, "lon": 2}]')
        with _resolving():
            markers = gateway.get_map_markers()

        self.assertEqual([marker.id for marker in markers], ["a"])


class ImmichSlowDripTests(TestCase):
    """Real sockets: a per-read timeout never fires against a server sending a byte every 50ms."""

    def test_the_deadline_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, "IMMICH_THUMBNAIL_DEADLINE_SECONDS"))

    @override_settings(IMMICH_THUMBNAIL_DEADLINE_SECONDS=1)
    def test_a_dripped_thumbnail_is_cut_at_the_deadline(self) -> None:
        url = start_drip_server(self, interval=0.05, body_bytes=400)
        gateway = ImmichGateway(account=_account(url.rstrip("/")), session=requests.Session())
        started = time.monotonic()
        with (
            mock.patch(
                "urbanlens.dashboard.services.security.url_safety.resolve_public_http_url",
                side_effect=lambda target, **_kwargs: (target, "127.0.0.1"),
            ),
            self.assertRaises(GatewayRequestError),
        ):
            gateway.get_asset_thumbnail("asset-1")

        self.assertLess(time.monotonic() - started, 4, "the per-read timeout was the only bound")
