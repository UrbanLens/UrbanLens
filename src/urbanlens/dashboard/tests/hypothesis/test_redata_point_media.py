"""REData's location context read, the shared per-point domain reads, and the media galleries built on them."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.redata_aerial_media import AerialMediaSource
from urbanlens.dashboard.plugins.builtin.redata_nearby_media import NearbyMediaSource, media_item_from_row
from urbanlens.dashboard.plugins.builtin.redata_street_level import StreetLevelPhotosSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)
from urbanlens.dashboard.services.apis.locations.redata_locations_context_gateway import (
    LocationsContext,
    RedataLocationsContextGateway,
)
from urbanlens.dashboard.services.apis.locations.redata_media_gateway import RedataMediaGateway
from urbanlens.dashboard.services.apis.locations.redata_street_view_gateway import RedataStreetViewGateway
from urbanlens.dashboard.services.locations import redata_point_data
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

_MEDIA_UUID = "6f1c2b9e-1d1a-4e0b-9a43-2f6f7a1b9c10"
_CAPTURE_UUID = "0b7f8d3e-55aa-4c11-8e2d-1c1f2a3b4c5d"


@contextmanager
def _redata_on() -> Iterator[None]:
    with (
        mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
        mock.patch.object(settings, "redata_api_key", "test-key"),
    ):
        yield


def _response(status_code: int, body: object, *, content: bytes = b"", content_type: str = "") -> mock.Mock:
    response = mock.Mock(
        status_code=status_code, text="", headers={"Content-Type": content_type} if content_type else {}
    )
    response.json.return_value = body
    response.raw.read.return_value = content
    response._content_consumed = False
    return response


def _media_row(**overrides: object) -> dict:
    """A row in REData's ``MediaItemSerializer`` shape."""
    row = {
        "uuid": _MEDIA_UUID,
        "provider": "wikimedia_commons",
        "external_id": "File:Mill.jpg",
        "kind": "photo",
        "title": "Old Mill from the river",
        "description": "",
        "url": "https://commons.wikimedia.org/wiki/File:Mill.jpg",
        "thumbnail_url": "https://upload.wikimedia.org/thumb/Mill.jpg",
        "cached_url": "",
        "credit": "Jane Doe",
        "is_live": None,
        "duration_seconds": None,
        "embed_url": "",
        "embed_kind": "",
        "embed_refresh_seconds": None,
        "embed_checked_at": None,
        "latitude": 41.7,
        "longitude": -73.9,
        "attributes": {"license": "CC BY-SA 4.0"},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
        "is_aerial": False,
        "confidence": None,
        "created": "2026-09-01T00:00:00Z",
        "updated": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _capture(provider: str, captured_on: str, latitude: float, longitude: float, **overrides: object) -> dict:
    """A row in REData's ``StreetViewCaptureSerializer`` shape."""
    row = {
        "uuid": _CAPTURE_UUID,
        "provider": provider,
        "external_id": f"{provider}-{captured_on}-{latitude}",
        "sequence_id": "seq-1",
        "latitude": latitude,
        "longitude": longitude,
        "captured_at": f"{captured_on}T12:00:00Z",
        "captured_on": captured_on,
        "heading_degrees": 90.0,
        "is_panoramic": False,
        "image_url": f"https://images.example.test/{provider}/{latitude}.jpg",
        "thumbnail_url": f"https://images.example.test/{provider}/{latitude}-thumb.jpg",
        "download_url": f"https://redata.example.test/api/v1/street-view/{_CAPTURE_UUID}/download/",
        "cached": False,
        "credit": "volunteer",
        "license": "CC BY-SA",
        "attributes": {"date_is_upload_time": False},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _envelope(results: list[dict], *, complete: bool = True) -> LocationContextEnvelope:
    return LocationContextEnvelope(count=len(results), complete=complete, results=results, providers=[])


class LocationsContextGatewayTests(SimpleTestCase):
    """``GET /locations/context/`` parsed against REData's ``LocationContextView`` body."""

    def _get_context(self, body: object) -> tuple[LocationsContext, mock.Mock]:
        session = mock.Mock()
        session.get.return_value = _response(200, body)
        gateway = RedataLocationsContextGateway(base_url="https://redata.example.test", api_key="k", session=session)
        return gateway.get_context(41.7, -73.9, ("media", "street_view")), session

    def test_asks_for_each_domain_and_settles_only_complete_ones(self) -> None:
        body = {
            "latitude": 41.7,
            "longitude": -73.9,
            "domains": {
                "media": {
                    "count": 1,
                    "complete": True,
                    "results": [_media_row()],
                    "providers": [
                        {
                            "provider": "wikimedia_commons",
                            "status": "ok",
                            "count": 1,
                            "message": "",
                            "radius_meters": 1000,
                            "limit": None,
                        }
                    ],
                },
                "street_view": {
                    "count": 0,
                    "complete": False,
                    "results": [],
                    "providers": [
                        {
                            "provider": "mapillary",
                            "status": "not_cached",
                            "count": 0,
                            "message": "Not cached",
                            "radius_meters": 100,
                            "limit": None,
                        }
                    ],
                    "error": "not_cached",
                    "message": "Not cached",
                },
            },
            "omitted": ["reference_documents"],
        }
        context, session = self._get_context(body)

        url = session.get.call_args.args[0]
        params = session.get.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/api/v1/locations/context/"))
        self.assertEqual(params["domain"], ["media", "street_view"])
        media = context.settled("media")
        assert media is not None
        self.assertEqual(media.results[0]["uuid"], _MEDIA_UUID)
        self.assertIsNone(context.settled("street_view"))
        self.assertIsNone(context.settled("historical_features"))
        self.assertEqual(context.omitted, ("reference_documents",))

    def test_a_body_without_domains_is_an_error_not_an_empty_answer(self) -> None:
        with pytest.raises(LocationContextUnavailableError):
            self._get_context({"error": "unexpected"})


class RedataFileDownloadTests(SimpleTestCase):
    def _gateway(self, response: mock.Mock) -> RedataMediaGateway:
        session = mock.Mock()
        session.get.return_value = response
        return RedataMediaGateway(base_url="https://redata.example.test", api_key="k", session=session)

    def test_returns_the_mirrored_bytes_and_type(self) -> None:
        gateway = self._gateway(_response(200, None, content=b"RIFFwebp", content_type="image/webp"))
        self.assertEqual(gateway.download(_MEDIA_UUID), (b"RIFFwebp", "image/webp"))
        self.assertTrue(gateway.session.get.call_args.args[0].endswith(f"/api/v1/media/{_MEDIA_UUID}/download/"))
        self.assertTrue(gateway.session.get.call_args.kwargs["stream"])

    def test_an_unmirrored_item_is_a_settled_refusal(self) -> None:
        gateway = self._gateway(_response(404, {"error": "media_not_cached", "message": "Use its url."}))
        with pytest.raises(LocationContextUnavailableError) as raised:
            gateway.download(_MEDIA_UUID)
        self.assertEqual(raised.value.reason, "media_not_cached")
        self.assertFalse(raised.value.is_outage)

    def test_a_503_is_an_outage(self) -> None:
        gateway = self._gateway(_response(503, {"error": "capture_unavailable", "message": "down"}))
        with pytest.raises(LocationContextUnavailableError) as raised:
            gateway.download(_MEDIA_UUID)
        self.assertTrue(raised.value.is_outage)


class SharedPointDataTests(SimpleTestCase):
    def test_without_redata_nothing_is_asked_of_the_context(self) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", None),
            mock.patch.object(RedataLocationsContextGateway, "get_context") as get_context,
        ):
            self.assertIsNone(redata_point_data.settled_domain(41.7, -73.9, "media"))
        get_context.assert_not_called()

    def test_one_context_read_answers_every_domain(self) -> None:
        context = LocationsContext(
            domains={"media": _envelope([_media_row()]), "street_view": _envelope([], complete=False)}
        )
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context) as get_context,
        ):
            self.assertIsNotNone(redata_point_data.settled_domain(41.7, -73.9, "media"))
            self.assertIsNone(redata_point_data.settled_domain(41.7, -73.9, "street_view"))
        get_context.assert_called_once_with(41.7, -73.9, redata_point_data.CONTEXT_DOMAINS)

    def test_a_settled_domain_skips_its_own_endpoint(self) -> None:
        context = LocationsContext(domains={"media": _envelope([_media_row()])})
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context),
            mock.patch.object(RedataMediaGateway, "lookup") as lookup,
        ):
            rows = redata_point_data.media_near(41.7, -73.9)
        lookup.assert_not_called()
        self.assertEqual([row["uuid"] for row in rows], [_MEDIA_UUID])

    def test_an_unsettled_domain_asks_its_endpoint_once_for_every_reader(self) -> None:
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataMediaGateway, "lookup", return_value=[_media_row()]) as lookup,
        ):
            redata_point_data.media_near(41.7, -73.9)
            redata_point_data.media_near(41.7, -73.9)
        lookup.assert_called_once_with(41.7, -73.9)

    def test_an_unreadable_context_falls_back_to_the_endpoint(self) -> None:
        with (
            _redata_on(),
            mock.patch.object(
                RedataLocationsContextGateway,
                "get_context",
                side_effect=LocationContextUnavailableError("source_error", "down"),
            ),
            mock.patch.object(RedataMediaGateway, "lookup", return_value=[]) as lookup,
        ):
            self.assertEqual(redata_point_data.media_near(41.7, -73.9), [])
        lookup.assert_called_once()

    def test_street_view_dates_come_from_cached_captures_with_the_nearest_frame(self) -> None:
        captures = [
            _capture("mapillary", "2019-05-03", 41.7009, -73.9),
            _capture("mapillary", "2019-05-03", 41.7001, -73.9, is_panoramic=True),
            _capture("panoramax", "2023-01-02", 41.7002, -73.9),
            _capture("kartaview", "", 41.7, -73.9),
        ]
        context = LocationsContext(domains={"street_view": _envelope(captures)})
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context),
            mock.patch.object(RedataStreetViewGateway, "get_timeline") as timeline,
        ):
            found = redata_point_data.street_view_dates(41.7, -73.9)
        timeline.assert_not_called()
        self.assertEqual(
            [(entry["provider"], entry["captured_on"]) for entry in found.dates],
            [("panoramax", "2023-01-02"), ("mapillary", "2019-05-03")],
        )
        mapillary = found.dates[1]
        self.assertEqual(mapillary["count"], 2)
        self.assertTrue(mapillary["is_panoramic"])
        self.assertEqual(mapillary["representative"]["latitude"], 41.7001)

    def test_street_view_dates_fall_back_to_one_timeline_for_every_network(self) -> None:
        timeline = {
            "dates": [
                {
                    "captured_on": "2015-01-01",
                    "provider": "kartaview",
                    "count": 1,
                    "is_panoramic": False,
                    "representative": _capture("kartaview", "2015-01-01", 41.7, -73.9),
                },
                {
                    "captured_on": "2021-06-01",
                    "provider": "mapillary",
                    "count": 4,
                    "is_panoramic": True,
                    "representative": _capture("mapillary", "2021-06-01", 41.7, -73.9),
                },
            ],
            "complete": False,
            "providers": [],
        }
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataStreetViewGateway, "get_timeline", return_value=timeline) as get_timeline,
        ):
            found = redata_point_data.street_view_dates(41.7, -73.9)
        get_timeline.assert_called_once_with(41.7, -73.9)
        self.assertEqual([entry["captured_on"] for entry in found.dates], ["2021-06-01", "2015-01-01"])
        self.assertFalse(found.complete)


def _pin(latitude: float = 41.7, longitude: float = -73.9):
    location = baker.make("dashboard.Location", latitude=latitude, longitude=longitude)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


class NearbyMediaSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = _pin()
        self.rows = [
            _media_row(
                uuid=_MEDIA_UUID, cached_url=f"https://redata.example.test/api/v1/media/{_MEDIA_UUID}/download/"
            ),
            _media_row(
                uuid="11111111-1111-4111-8111-111111111111",
                provider="youtube",
                is_aerial=True,
                title="Drone over the mill",
            ),
            _media_row(uuid="22222222-2222-4222-8222-222222222222", provider="mapillary", title="street frame"),
            _media_row(
                uuid="33333333-3333-4333-8333-333333333333",
                provider="flickr",
                thumbnail_url="",
                cached_url="",
                title="no picture",
            ),
        ]

    def test_gated_off_without_redata(self) -> None:
        with mock.patch.object(settings, "redata_api_url", None):
            self.assertFalse(NearbyMediaSource().gate(self.pin))
            self.assertFalse(AerialMediaSource().gate(self.pin))
            self.assertFalse(StreetLevelPhotosSource().gate(self.pin))

    def test_one_lookup_fills_the_nearby_and_aerial_tabs(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataMediaGateway, "lookup", return_value=self.rows) as lookup,
        ):
            NearbyMediaSource().fetch(self.pin)
            AerialMediaSource().fetch(self.pin)
        lookup.assert_called_once()

        nearby = LocationCache.get_fresh(self.pin.location, "redata_media")
        aerial = LocationCache.get_fresh(self.pin.location, "redata_aerial")
        assert nearby is not None and aerial is not None
        self.assertEqual([row["provider"] for row in nearby.data["items"]], ["wikimedia_commons", "flickr"])
        self.assertNotIn("attributes", nearby.data["items"][0])
        self.assertEqual([row["title"] for row in aerial.data["items"]], ["Drone over the mill"])

    def test_a_mirrored_image_is_read_through_this_sites_proxy(self) -> None:
        item = media_item_from_row(
            _media_row(cached_url=f"https://redata.example.test/api/v1/media/{_MEDIA_UUID}/download/")
        )
        assert item is not None
        self.assertEqual(item.url, reverse("pin.redata.media", args=[_MEDIA_UUID]))
        self.assertNotIn("redata.example.test", item.thumb_url)
        self.assertEqual(item.page_url, "https://commons.wikimedia.org/wiki/File:Mill.jpg")
        self.assertEqual(item.source, "Wikimedia Commons")
        self.assertEqual(item.author, "Jane Doe")

    def test_an_unmirrored_row_shows_its_thumbnail_and_a_pictureless_row_none(self) -> None:
        source = NearbyMediaSource()
        items = source.media_items({"items": [_media_row(), _media_row(thumbnail_url="", title="audio")]})
        self.assertEqual([item.url for item in items], ["https://upload.wikimedia.org/thumb/Mill.jpg"])

    def test_items_far_from_the_place_and_not_naming_it_are_judged_irrelevant(self) -> None:
        """Commons searches a kilometre around the point; a frame of another street is not about this place."""
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        self.pin.name = "Smalltown Paper Mill"
        self.pin.save()
        elsewhere = _media_row(title="File:IMG_2041.jpg", latitude=45.0, longitude=-80.0)
        named = _media_row(title="Smalltown Paper Mill, 1910", latitude=None, longitude=None)
        items = NearbyMediaSource().gallery_items({"items": [elsewhere, named]}, subject_for_pin(self.pin))
        self.assertEqual([item.caption for item in items], ["Smalltown Paper Mill, 1910"])


class StreetLevelPhotosSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = _pin()
        self.source = StreetLevelPhotosSource()

    def test_fetch_caches_each_date_once_and_media_items_open_the_archive(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        found = redata_point_data.StreetViewDates(
            dates=[
                {
                    "captured_on": "2021-06-01",
                    "provider": "mapillary",
                    "count": 4,
                    "is_panoramic": True,
                    "representative": _capture("mapillary", "2021-06-01", 41.7, -73.9),
                }
            ],
        )
        with _redata_on(), mock.patch.object(redata_point_data, "street_view_dates", return_value=found):
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "redata_street_level")
        assert cached is not None
        self.assertNotIn("download_url", cached.data["dates"][0]["representative"])

        items = self.source.media_items(cached.data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, reverse("pin.redata.street_view", args=[_CAPTURE_UUID]))
        self.assertEqual(items[0].thumb_url, "https://images.example.test/mapillary/41.7-thumb.jpg")
        self.assertEqual(items[0].caption, "Mapillary · 2021-06-01 · 360° · facing 90°")

    def test_an_outage_is_not_cached_as_no_photos(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with (
            _redata_on(),
            mock.patch.object(
                redata_point_data, "street_view_dates", return_value=redata_point_data.StreetViewDates(complete=False)
            ),
        ):
            self.source.fetch(self.pin)
        self.assertIsNone(LocationCache.get_fresh(self.pin.location, "redata_street_level"))


class RedataMediaProxyTests(SimpleTestCase):
    def test_serves_the_mirrored_bytes(self) -> None:
        with (
            mock.patch.object(RedataMediaGateway, "download", return_value=(b"RIFF....WEBPVP8 ", "image/webp")),
            _redata_on(),
        ):
            response = self.client.get(reverse("pin.redata.media", args=[_MEDIA_UUID]))
        self.assertEqual(response.status_code, 200)

    def test_an_unmirrored_item_is_a_404(self) -> None:
        with (
            _redata_on(),
            mock.patch.object(
                RedataMediaGateway,
                "download",
                side_effect=LocationContextUnavailableError("media_not_cached", "", rejected=True),
            ),
        ):
            response = self.client.get(reverse("pin.redata.media", args=[_MEDIA_UUID]))
        self.assertEqual(response.status_code, 404)

    def test_without_redata_the_street_view_proxy_is_a_404(self) -> None:
        with mock.patch.object(settings, "redata_api_url", None):
            response = self.client.get(reverse("pin.redata.street_view", args=[_CAPTURE_UUID]))
        self.assertEqual(response.status_code, 404)
