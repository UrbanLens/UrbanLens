"""Tests for the public "Import a Flickr Album" feature (pin + wiki Media).

Distinct from test_flickr.py, which covers the per-user OAuth "Import from
Flickr" picker (one user's own connected library). This covers the
URL-based, unauthenticated public-album import instead:

- parse_album_url - accepts current/legacy album URL shapes, rejects others.
- FlickrPublicGateway.get_album/download_photo - unsigned public API calls,
  NSID vs. path-alias resolution, error mapping.
- PinFlickrAlbumLookupView/ImportView and their Wiki counterparts.
- import_flickr_album_photos task - pin and wiki targets, checksum dedupe,
  quota enforcement, no log_visit_on_pin call (these are someone else's
  photos, not the profile's own visit evidence).

All HTTP calls are mocked; no real network access occurs.
"""

from __future__ import annotations

import hashlib
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbumPhoto, FlickrPublicGateway, parse_album_url, photo_web_url
from urbanlens.dashboard.services.gateway import GatewayRequestError


def _mock_response(*, ok: bool = True, status_code: int = 200, json_data=None, content: bytes = b"", headers: dict | None = None):
    resp = mock.MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = content
    resp.headers = headers or {}
    resp.text = ""
    return resp


_GET_PHOTOS_OK = {
    "stat": "ok",
    "photoset": {
        "id": "72177720000000001",
        "ownername": "somebody",
        "total": "2",
        "photo": [
            {"id": "1", "title": "First", "url_z": "https://example.com/1_z.jpg", "url_o": "https://example.com/1_o.jpg", "ownername": "somebody", "datetaken": "2024-01-01 00:00:00"},
            {"id": "2", "title": "Second", "url_z": "https://example.com/2_z.jpg", "ownername": "somebody", "datetaken": "2024-01-02 00:00:00"},
        ],
    },
}
_GET_INFO_OK = {"stat": "ok", "photoset": {"id": "72177720000000001", "username": "somebody", "title": {"_content": "My Album"}}}


class ParseAlbumUrlTests(SimpleTestCase):
    """parse_album_url accepts both current and legacy Flickr album URL shapes."""

    def test_albums_path_with_nsid(self) -> None:
        result = parse_album_url("https://www.flickr.com/photos/12345678@N00/albums/72177720000000001")
        self.assertEqual(result, ("12345678@N00", "72177720000000001"))

    def test_legacy_sets_path_with_alias(self) -> None:
        result = parse_album_url("https://www.flickr.com/photos/someuser/sets/72177720000000001/")
        self.assertEqual(result, ("someuser", "72177720000000001"))

    def test_trailing_query_string_ignored(self) -> None:
        result = parse_album_url("https://www.flickr.com/photos/someuser/albums/72177720000000001?foo=bar")
        self.assertEqual(result, ("someuser", "72177720000000001"))

    def test_non_flickr_url_returns_none(self) -> None:
        self.assertIsNone(parse_album_url("https://example.com/photos/someuser/albums/1"))

    def test_photo_page_url_without_album_returns_none(self) -> None:
        self.assertIsNone(parse_album_url("https://www.flickr.com/photos/someuser/1234567890/"))


class PhotoWebUrlTests(SimpleTestCase):
    def test_matches_flickr_account_photo_web_url_format(self) -> None:
        self.assertEqual(photo_web_url("12345678@N00", "42"), "https://www.flickr.com/photos/12345678@N00/42/")


class FlickrPublicGatewayTests(TestCase):
    """get_album/download_photo - unsigned public calls, NSID resolution, error mapping."""

    def _gateway(self) -> FlickrPublicGateway:
        return FlickrPublicGateway(session=mock.MagicMock())

    def test_get_album_with_raw_nsid_skips_lookup_call(self) -> None:
        gw = self._gateway()
        gw.session.get.side_effect = [_mock_response(json_data=_GET_PHOTOS_OK), _mock_response(json_data=_GET_INFO_OK)]
        with mock.patch("urbanlens.dashboard.services.apis.flickr.public._consumer_credentials", return_value=("key", "secret")):
            album = gw.get_album("https://www.flickr.com/photos/12345678@N00/albums/72177720000000001")

        self.assertEqual(gw.session.get.call_count, 2)  # getPhotos + getInfo, no lookupUser
        self.assertEqual(album.title, "My Album")
        self.assertEqual(album.owner_nsid, "12345678@N00")
        self.assertEqual(album.owner_username, "somebody")
        self.assertEqual(album.total, 2)
        self.assertEqual([p.id for p in album.photos], ["1", "2"])
        self.assertEqual(album.photos[0].download_url, "https://example.com/1_o.jpg")
        # No url_o for photo 2 - falls back to the largest available extras field.
        self.assertEqual(album.photos[1].download_url, "https://example.com/2_z.jpg")

    def test_get_album_with_path_alias_resolves_nsid_first(self) -> None:
        gw = self._gateway()
        gw.session.get.side_effect = [
            _mock_response(json_data={"stat": "ok", "user": {"id": "12345678@N00"}}),
            _mock_response(json_data=_GET_PHOTOS_OK),
            _mock_response(json_data=_GET_INFO_OK),
        ]
        with mock.patch("urbanlens.dashboard.services.apis.flickr.public._consumer_credentials", return_value=("key", "secret")):
            album = gw.get_album("https://www.flickr.com/photos/someuser/albums/72177720000000001")
        self.assertEqual(gw.session.get.call_count, 3)  # lookupUser + getPhotos + getInfo
        self.assertEqual(album.owner_nsid, "12345678@N00")

    def test_malformed_url_raises_value_error(self) -> None:
        gw = self._gateway()
        with self.assertRaises(ValueError):
            gw.get_album("https://example.com/not-flickr")

    def test_private_or_missing_album_raises_gateway_error(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(json_data={"stat": "fail", "message": "Photoset not found"})
        with mock.patch("urbanlens.dashboard.services.apis.flickr.public._consumer_credentials", return_value=("key", "secret")), self.assertRaises(GatewayRequestError):
            gw.get_album("https://www.flickr.com/photos/12345678@N00/albums/1")

    def test_http_error_status_raises(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(ok=False, status_code=500)
        with mock.patch("urbanlens.dashboard.services.apis.flickr.public._consumer_credentials", return_value=("key", "secret")), self.assertRaises(GatewayRequestError):
            gw.get_album("https://www.flickr.com/photos/12345678@N00/albums/1")

    def test_download_photo_returns_bytes(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(content=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"})
        photo = FlickrAlbumPhoto(id="1", title="", thumbnail_url=None, download_url="https://example.com/1_o.jpg", author=None, taken_at=None)
        content, _filename, content_type = gw.download_photo(photo)
        self.assertEqual(content, b"jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")

    def test_download_photo_without_url_raises(self) -> None:
        gw = self._gateway()
        photo = FlickrAlbumPhoto(id="1", title="", thumbnail_url=None, download_url=None, author=None, taken_at=None)
        with self.assertRaises(GatewayRequestError):
            gw.download_photo(photo)


class PinFlickrAlbumLookupViewTests(TestCase):
    """Lookup view: renders errors, or a preview grid flagging already-imported photos."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_blank_url_shows_an_error(self) -> None:
        response = self.client.post(reverse("pin.flickr_album.lookup", args=[self.pin.slug]), {"album_url": ""})
        self.assertContains(response, "Paste a Flickr album URL")

    def test_not_configured_shows_an_error(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.flickr.flickr_is_configured", return_value=False):
            response = self.client.post(reverse("pin.flickr_album.lookup", args=[self.pin.slug]), {"album_url": "https://www.flickr.com/photos/x/albums/1"})
        self.assertContains(response, "not configured")

    def test_valid_album_flags_already_imported_photo(self) -> None:
        baker.make(Image, pin=self.pin, profile=self.profile, source_url=photo_web_url("12345678@N00", "1"))
        album_url = "https://www.flickr.com/photos/12345678@N00/albums/72177720000000001"
        with (
            mock.patch("urbanlens.dashboard.controllers.flickr.flickr_is_configured", return_value=True),
            mock.patch.object(FlickrPublicGateway, "get_album") as mock_get_album,
        ):
            mock_get_album.return_value = mock.MagicMock(
                title="My Album",
                owner_nsid="12345678@N00",
                owner_username="somebody",
                total=2,
                photos=[
                    FlickrAlbumPhoto(id="1", title="", thumbnail_url="https://example.com/1_z.jpg", download_url="https://example.com/1_o.jpg", author=None, taken_at=None),
                    FlickrAlbumPhoto(id="2", title="", thumbnail_url="https://example.com/2_z.jpg", download_url="https://example.com/2_o.jpg", author=None, taken_at=None),
                ],
            )
            response = self.client.post(reverse("pin.flickr_album.lookup", args=[self.pin.slug]), {"album_url": album_url})
        assets = response.context["assets"]
        self.assertTrue(assets[0]["already_imported"])
        self.assertFalse(assets[1]["already_imported"])

    def test_gateway_error_is_shown(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.controllers.flickr.flickr_is_configured", return_value=True),
            mock.patch.object(FlickrPublicGateway, "get_album", side_effect=GatewayRequestError("Photoset not found")),
        ):
            response = self.client.post(reverse("pin.flickr_album.lookup", args=[self.pin.slug]), {"album_url": "https://www.flickr.com/photos/x/albums/1"})
        self.assertContains(response, "Photoset not found")


class PinFlickrAlbumImportViewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_no_photos_selected_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.flickr_album.import", args=[self.pin.slug]), {"album_url": "https://www.flickr.com/photos/x/albums/1"})
        self.assertEqual(response.status_code, 400)

    def test_enqueues_the_import_task(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.flickr.safely_enqueue_task") as mock_enqueue:
            mock_enqueue.return_value = mock.MagicMock(id="task-123")
            response = self.client.post(
                reverse("pin.flickr_album.import", args=[self.pin.slug]),
                {"album_url": "https://www.flickr.com/photos/x/albums/1", "photo_ids": ["1", "2"]},
            )
        self.assertEqual(response.status_code, 200)
        args, _kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], tasks.import_flickr_album_photos)
        self.assertEqual(args[1:], ("pin", self.pin.pk, self.profile.pk, "https://www.flickr.com/photos/x/albums/1", ["1", "2"]))


class WikiFlickrAlbumViewTests(TestCase):
    """The wiki variants require the requester to have pinned the wiki's location."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def test_unpinned_location_is_a_404(self) -> None:
        other_location = baker.make("dashboard.Location")
        other_wiki = baker.make("dashboard.Wiki", location=other_location)
        response = self.client.post(reverse("location.wiki.flickr_album.lookup", args=[other_wiki.location.slug]), {"album_url": "https://www.flickr.com/photos/x/albums/1"})
        self.assertEqual(response.status_code, 404)

    def test_enqueues_the_import_task_with_wiki_target(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.flickr.safely_enqueue_task") as mock_enqueue:
            mock_enqueue.return_value = mock.MagicMock(id="task-123")
            response = self.client.post(
                reverse("location.wiki.flickr_album.import", args=[self.location.slug]),
                {"album_url": "https://www.flickr.com/photos/x/albums/1", "photo_ids": ["1"]},
            )
        self.assertEqual(response.status_code, 200)
        args, _kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], tasks.import_flickr_album_photos)
        self.assertEqual(args[1:], ("wiki", self.wiki.pk, self.profile.pk, "https://www.flickr.com/photos/x/albums/1", ["1"]))


class ImportFlickrAlbumPhotosTaskTests(TestCase):
    """import_flickr_album_photos - pin/wiki targets, dedupe, quota, no visit logging."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location")
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        self.album_url = "https://www.flickr.com/photos/12345678@N00/albums/1"
        self.photo = FlickrAlbumPhoto(id="1", title="A Photo", thumbnail_url="https://example.com/1_z.jpg", download_url="https://example.com/1_o.jpg", author="somebody", taken_at=None)

    def _fake_album(self, photos=None):
        return mock.MagicMock(owner_nsid="12345678@N00", photos=photos if photos is not None else [self.photo])

    def test_imports_onto_a_pin(self) -> None:
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", return_value=(b"jpeg-bytes", "1.jpg", "image/jpeg")),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["1"])

        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertEqual(image.source, ImageSource.FLICKR)
        self.assertEqual(image.author, "somebody")
        self.assertEqual(image.source_url, "https://www.flickr.com/photos/12345678@N00/1/")
        self.assertIsNone(image.wiki_id)

    def test_imports_onto_a_wiki(self) -> None:
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", return_value=(b"jpeg-bytes", "1.jpg", "image/jpeg")),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_flickr_album_photos("wiki", self.wiki.pk, self.profile.pk, self.album_url, ["1"])

        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(wiki=self.wiki, profile=self.profile)
        self.assertIsNone(image.pin_id)

    def test_does_not_log_a_visit(self) -> None:
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", return_value=(b"jpeg-bytes", "1.jpg", "image/jpeg")),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch("urbanlens.dashboard.services.memories.photos.log_visit_on_pin") as mock_log_visit,
        ):
            tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["1"])
        mock_log_visit.assert_not_called()

    def test_duplicate_checksum_is_skipped(self) -> None:
        checksum = hashlib.sha256(b"jpeg-bytes").hexdigest()
        baker.make(Image, pin=self.pin, profile=self.profile, checksum=checksum, image=ContentFile(b"existing", name="existing.jpg"))
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", return_value=(b"jpeg-bytes", "1.jpg", "image/jpeg")),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 1, "failed": 0})

    def test_download_failure_is_counted_as_failed(self) -> None:
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", side_effect=GatewayRequestError("boom")),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 1})

    def test_quota_exceeded_is_counted_as_failed(self) -> None:
        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()),
            mock.patch.object(FlickrPublicGateway, "download_photo", return_value=(b"jpeg-bytes", "1.jpg", "image/jpeg")),
            mock.patch("urbanlens.dashboard.services.storage.quota_error_for_upload", return_value="Storage full."),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 1})

    def test_missing_pin_returns_empty_counts_without_crashing(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_flickr_album_photos("pin", 0, self.profile.pk, self.album_url, ["1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})

    def test_unresolvable_photo_ids_are_silently_dropped(self) -> None:
        with mock.patch.object(FlickrPublicGateway, "get_album", return_value=self._fake_album()), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_flickr_album_photos("pin", self.pin.pk, self.profile.pk, self.album_url, ["does-not-exist"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})
