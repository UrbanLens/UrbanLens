"""
The media proxies and Flickr album lookup run under the request-path policy.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.views import View
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.proxied_media import ProxiedMediaMixin
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbum, FlickrPublicGateway

_ALBUM_URL = "https://www.flickr.com/photos/12345678@N00/albums/72177720000000001"


def _match(georeference_uuid: str) -> dict:
    return {
        "sheet": {"title": "Sanborn Fire Insurance Map", "date_text": "1893", "kind": "fire_insurance"},
        "georeference": {"uuid": georeference_uuid, "bounds": [-71.06, 42.35, -71.05, 42.36]},
        "contains_point": True,
    }


class _Case(TestCase):
    upstreams: tuple[type[request_upstreams.RequestUpstream], ...] = ()

    def setUp(self) -> None:
        super().setUp()
        for upstream in self.upstreams:
            upstream.reset()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _hang(self, *_args, **_kwargs):
        self.release.wait(timeout=10)
        return (b"late", "image/jpeg")


class _FakeProxyView(ProxiedMediaMixin, View):
    """A proxy over a download the test controls."""

    download = staticmethod(lambda: (b"photo", "image/jpeg"))

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.serve_media(request, "ul_fake_proxy_1", type(self).download)


class ProxiedMediaSlotTests(_Case):
    upstreams = (request_upstreams.ProxiedMediaUpstream,)

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()
        self.view = _FakeProxyView.as_view()

    def _get(self) -> HttpResponse:
        return self.view(self.factory.get("/proxy/"))

    def test_with_every_slot_taken_the_proxy_answers_503_without_downloading(self) -> None:
        semaphore = request_upstreams.ProxiedMediaUpstream.semaphore()
        taken = 0
        while semaphore.acquire(blocking=False):
            taken += 1
        self.addCleanup(lambda: [semaphore.release() for _ in range(taken)])
        download = mock.Mock()

        with mock.patch.object(_FakeProxyView, "download", staticmethod(download)):
            response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        download.assert_not_called()

    def test_a_download_that_outlives_the_request_is_still_cached(self) -> None:
        from urbanlens.dashboard.services.core import bounded_cache

        download = mock.Mock(side_effect=self._hang)
        with (
            mock.patch.object(request_upstreams.ProxiedMediaUpstream, "deadline", 0.2),
            mock.patch.object(_FakeProxyView, "download", staticmethod(download)),
        ):
            timed_out = self._get()
            self.release.set()
            deadline = time.monotonic() + 5
            while bounded_cache.get_or_none("ul_fake_proxy_1", label="test") is None and time.monotonic() < deadline:
                time.sleep(0.02)
            response = self._get()

        self.assertEqual(timed_out.status_code, 503)
        self.assertEqual((response.status_code, bytes(response.content)), (200, b"late"))
        self.assertEqual(download.call_count, 1)

    def test_an_unexpected_upstream_failure_is_a_502_not_a_500(self) -> None:
        import requests

        with mock.patch.object(
            _FakeProxyView, "download", staticmethod(mock.Mock(side_effect=requests.ConnectionError("down")))
        ):
            response = self._get()

        self.assertEqual(response.status_code, 502)

    def test_an_error_the_view_names_unavailable_is_a_404(self) -> None:
        class GoneError(Exception):
            pass

        class Gated(_FakeProxyView):
            unavailable_errors = (GoneError,)

        with mock.patch.object(Gated, "download", staticmethod(mock.Mock(side_effect=GoneError("no such file")))):
            response = Gated.as_view()(self.factory.get("/proxy/"))

        self.assertEqual(response.status_code, 404)


class FlickrAlbumLookupTests(_Case):
    upstreams = (request_upstreams.FlickrAlbumUpstream,)

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.url = reverse("pin.flickr_album.lookup", args=[self.pin.slug])
        patcher = mock.patch("urbanlens.dashboard.controllers.flickr.flickr_is_configured", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _album(self) -> FlickrAlbum:
        return FlickrAlbum(
            photoset_id="72177720000000001",
            owner_nsid="12345678@N00",
            title="My Album",
            owner_username="somebody",
            total=0,
            photos=[],
        )

    def test_the_same_album_is_resolved_once(self) -> None:
        with mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._album()) as get_album:
            self.client.post(self.url, {"album_url": _ALBUM_URL})
            second = self.client.post(self.url, {"album_url": _ALBUM_URL + "?foo=1"})

        self.assertEqual(get_album.call_count, 1)
        self.assertIn("My Album", second.content.decode())

    def test_a_hanging_flickr_costs_the_request_only_the_deadline(self) -> None:
        started = time.monotonic()
        with (
            mock.patch.object(request_upstreams.FlickrAlbumUpstream, "deadline", 0.2),
            mock.patch.object(FlickrPublicGateway, "get_album", side_effect=self._hang),
        ):
            body = self.client.post(self.url, {"album_url": _ALBUM_URL}).content.decode()

        self.assertIn("didn&#x27;t answer in time", body)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_malformed_url_still_explains_itself(self) -> None:
        with mock.patch.object(
            FlickrPublicGateway, "get_album", side_effect=ValueError("That isn't a Flickr album URL.")
        ):
            body = self.client.post(self.url, {"album_url": "https://example.com/x"}).content.decode()

        self.assertIn("a Flickr album URL", body)
