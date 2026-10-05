"""Historical-map browse, the REData media proxies and Flickr album lookup run under the request-path policy.

N29 G5-34 (browse waited up to 30 s on REData with no cache; the POST re-queried it), G6-3 (the REData media
proxies had no concurrency slot, unlike the tile proxies) and G6-8 (album lookup made three sequential
30 s calls with no overall deadline).
"""

from __future__ import annotations

import threading
import time
from unittest import mock
import uuid as uuid_module

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbum, FlickrPublicGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.security.throttle import Rate
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_MAPS_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway.RedataHistoricalMapsGateway"
_MAPS_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"
_LOOPNET_DOWNLOAD = (
    "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway.download_listing_photo"
)
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


class HistoricalMapBrowseTests(_Case):
    upstreams = (request_upstreams.HistoricalMapsBrowseUpstream,)

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=42.355, longitude=-71.055)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile, location=location)
        self.url = reverse("pin.overlays.historical", args=[self.pin.slug])
        self.georeference_uuid = str(uuid_module.uuid4())
        for patcher in (mock.patch(_MAPS_CONFIGURED, return_value=True),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_adding_a_sheet_reuses_the_list_the_browse_fetched(self) -> None:
        with mock.patch(_MAPS_GATEWAY) as gateway_cls:
            gateway_cls.return_value.get_maps_covering.return_value = [_match(self.georeference_uuid)]
            self.client.get(self.url)
            self.client.post(self.url, data={"georeference_uuid": self.georeference_uuid})

        self.assertEqual(gateway_cls.return_value.get_maps_covering.call_count, 1)
        self.assertTrue(MapImageOverlay.objects.filter(parent_pin=self.pin).exists())

    def test_an_outage_is_shown_and_not_cached(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        with mock.patch(_MAPS_GATEWAY) as gateway_cls:
            gateway_cls.return_value.get_maps_covering.side_effect = [
                LocationContextUnavailableError("source_error", "down"),
                [_match(self.georeference_uuid)],
            ]
            failed = self.client.get(self.url).content.decode()
            recovered = self.client.get(self.url).content.decode()

        self.assertIn("temporarily unavailable", failed)
        self.assertIn(self.georeference_uuid, recovered)

    def test_a_hanging_redata_costs_the_request_only_the_deadline(self) -> None:
        started = time.monotonic()
        with (
            mock.patch.object(request_upstreams.HistoricalMapsBrowseUpstream, "deadline", 0.2),
            mock.patch(_MAPS_GATEWAY) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.side_effect = self._hang
            body = self.client.get(self.url).content.decode()

        self.assertIn("temporarily unavailable", body)
        self.assertLess(time.monotonic() - started, 3)

    def test_one_account_is_throttled(self) -> None:
        with (
            mock.patch.object(request_upstreams.HistoricalMapsBrowseUpstream, "rate", Rate(limit=1, window_seconds=60)),
            mock.patch(_MAPS_GATEWAY) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.side_effect = GatewayRequestError("down")
            self.client.get(self.url)
            body = self.client.get(self.url).content.decode()

        self.assertIn("Too many historical map searches", body)
        self.assertEqual(gateway_cls.return_value.get_maps_covering.call_count, 1)


class RedataMediaSlotTests(RedataConfiguredMixin, _Case):
    upstreams = (request_upstreams.RedataMediaUpstream,)

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("pin.loopnet.photo", kwargs={"listing_uuid": "abc", "photo_id": 1})

    def test_with_every_slot_taken_the_proxy_answers_503_without_downloading(self) -> None:
        semaphore = request_upstreams.RedataMediaUpstream.semaphore()
        taken = 0
        while semaphore.acquire(blocking=False):
            taken += 1
        self.addCleanup(lambda: [semaphore.release() for _ in range(taken)])

        with mock.patch(_LOOPNET_DOWNLOAD) as download:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        download.assert_not_called()

    def test_a_download_that_outlives_the_request_is_still_cached(self) -> None:
        from urbanlens.dashboard.services.core import bounded_cache

        with (
            mock.patch.object(request_upstreams.RedataMediaUpstream, "deadline", 0.2),
            mock.patch(_LOOPNET_DOWNLOAD, side_effect=self._hang) as download,
        ):
            timed_out = self.client.get(self.url)
            self.release.set()
            deadline = time.monotonic() + 5
            while (
                bounded_cache.get_or_none("ul_loopnet_photo_abc_1", label="test") is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            response = self.client.get(self.url)

        self.assertEqual(timed_out.status_code, 503)
        self.assertEqual((response.status_code, bytes(response.content)), (200, b"late"))
        self.assertEqual(download.call_count, 1)

    def test_an_unexpected_upstream_failure_is_a_502_not_a_500(self) -> None:
        import requests

        with mock.patch(_LOOPNET_DOWNLOAD, side_effect=requests.ConnectionError("down")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 502)


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
