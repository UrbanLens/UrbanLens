"""Photo originals and Google's fixed-size map images are read through ``read_capped`` (Spark P356).

Each was ``response.content``, which buffers whatever the far end sends into the worker. A photo original is a file the
site is about to store, so it is bounded by the upload limit a browser upload gets; a Static Map or Street View image
has a size the request fixes, so its ceiling is small.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway
from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbumPhoto, FlickrPublicGateway
from urbanlens.dashboard.services.apis.locations.google import maps
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.apis.photos import google as google_photos
from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway
from urbanlens.dashboard.services.core.gateway import MAX_PROXIED_MEDIA_BYTES, GatewayRequestError
from urbanlens.dashboard.tests.hypothesis.test_proxied_media_is_capped import _streamed

_UPLOAD_CAP = 4096
_CAP = "urbanlens.dashboard.services.media.storage.max_upload_file_size_bytes"


def _json_response(payload: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.json.return_value = payload
    return response


class PhotoOriginalsAreBoundedByTheUploadLimitTests(TestCase):
    """A file the site would refuse from a browser is not one it should accept from Flickr or Google Photos."""

    def setUp(self) -> None:
        super().setUp()
        profile = baker.make(User).profile
        patcher = mock.patch(_CAP, return_value=_UPLOAD_CAP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.flickr = FlickrGateway(
            account=FlickrAccount(
                profile=profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00", flickr_username="u"
            ),
            session=mock.MagicMock(),
        )
        self.public = FlickrPublicGateway(session=mock.MagicMock())
        self.photo = FlickrAlbumPhoto(
            id="1", title="", thumbnail_url=None, download_url="https://example.com/1_o.jpg", author=None, taken_at=None
        )
        account = GooglePhotosAccount(
            profile=profile,
            access_token="a",
            refresh_token="r",
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.google = GooglePhotosGateway(account=account, session=mock.MagicMock())

    def _downloads(self, body: bytes) -> dict[str, tuple[mock.MagicMock, object]]:
        for gateway in (self.flickr, self.public, self.google):
            gateway.session.get.return_value = _streamed(body)
        return {
            "flickr original": (
                self.flickr.session,
                lambda: self.flickr.get_original("1", fallback_url="https://example.com/1_o.jpg")[0],
            ),
            "flickr album photo": (self.public.session, lambda: self.public.download_photo(self.photo)[0]),
            "google photos original": (
                self.google.session,
                lambda: self.google.download_media_item("https://x/a", original=True),
            ),
        }

    def test_a_photo_within_the_limit_arrives_whole_and_is_streamed(self) -> None:
        body = b"x" * _UPLOAD_CAP
        for name, (session, download) in self._downloads(body).items():
            with self.subTest(name):
                self.assertEqual(download(), body)
                self.assertTrue(
                    session.get.call_args.kwargs.get("stream"),
                    f"{name} was not streamed, so its size cannot be bounded",
                )

    def test_a_photo_over_the_limit_is_refused_as_a_gateway_error(self) -> None:
        """A GatewayRequestError is what the import loops count as one failed photo and carry on from."""
        for name, (_session, download) in self._downloads(b"x" * (_UPLOAD_CAP + 1)).items():
            with self.subTest(name), pytest.raises(GatewayRequestError, match="larger than"):
                download()

    def test_a_google_photos_preview_keeps_the_default_proxy_ceiling(self) -> None:
        """The picker's thumbnail is proxied to the browser, not stored, so the upload limit is not its measure."""
        self.google.session.get.return_value = _streamed(b"x" * (_UPLOAD_CAP + 1))
        self.assertEqual(len(self.google.download_media_item("https://x/a", original=False)), _UPLOAD_CAP + 1)

        self.google.session.get.return_value = _streamed(b"x" * (MAX_PROXIED_MEDIA_BYTES + 1))
        with pytest.raises(GatewayRequestError, match="larger than"):
            self.google.download_media_item("https://x/a", original=False)


class FixedSizeMapImagesHaveASmallCeilingTests(TestCase):
    def _gateway(self, **responses: object) -> GoogleMapsGateway:
        session = mock.MagicMock()
        session.get.side_effect = lambda url, **_kwargs: responses[url.rsplit("/", 1)[-1]]
        return GoogleMapsGateway(api_key="key", session=session)

    def test_a_static_map_is_streamed_and_arrives_whole(self) -> None:
        gateway = self._gateway(staticmap=_streamed(b"j" * 2048))

        self.assertEqual(gateway.get_satellite_image_bytes(1.0, 2.0), b"j" * 2048)
        self.assertTrue(gateway.session.get.call_args.kwargs.get("stream"))

    def test_a_static_map_over_the_ceiling_is_refused(self) -> None:
        gateway = self._gateway(staticmap=_streamed(b"j" * (maps._MAX_FIXED_SIZE_IMAGE_BYTES + 1)))

        with pytest.raises(GatewayRequestError, match="larger than"):
            gateway.get_satellite_image_bytes(1.0, 2.0)

    def test_the_ceiling_is_far_below_the_proxy_default(self) -> None:
        """A 640x640 JPEG is a few hundred kilobytes; the shared default is sized for a full-resolution photo."""
        self.assertLessEqual(maps._MAX_FIXED_SIZE_IMAGE_BYTES, MAX_PROXIED_MEDIA_BYTES // 2)

    def _street_view(self, image: object) -> GoogleMapsGateway:
        metadata = _json_response(
            {"status": "OK", "pano_id": "p1", "date": "2024-01", "location": {"lat": 1.0, "lng": 2.0}}
        )
        return self._gateway(metadata=metadata, streetview=image)

    def test_a_street_view_image_is_streamed_and_arrives_whole(self) -> None:
        gateway = self._street_view(_streamed(b"j" * 3000))

        image, date, latitude, longitude = gateway.get_street_view_single(1.0, 2.0)

        self.assertEqual((image, date, latitude, longitude), (b"j" * 3000, "2024-01", 1.0, 2.0))
        self.assertTrue(gateway.session.get.call_args.kwargs.get("stream"))

    def test_a_street_view_image_over_the_ceiling_is_refused(self) -> None:
        gateway = self._street_view(_streamed(b"j" * (maps._MAX_FIXED_SIZE_IMAGE_BYTES + 1)))

        with pytest.raises(GatewayRequestError, match="larger than"):
            gateway.get_street_view_single(1.0, 2.0)

    def test_a_tiny_street_view_image_is_still_the_placeholder_not_a_hit(self) -> None:
        """The 2000-byte placeholder check reads the capped body, so it must still see the bytes."""
        from urbanlens.dashboard.services.apis.locations.google.maps import StreetViewNotFoundError

        gateway = self._street_view(_streamed(b"j" * 100))

        with pytest.raises(StreetViewNotFoundError):
            gateway.get_street_view_single(1.0, 2.0)


class PickerListingIsBoundedTests(TestCase):
    """Spark P364: ``list_session_media_items`` was ``while True`` on whatever ``nextPageToken`` Google returned."""

    def test_a_listing_that_never_ends_stops_at_the_page_cap_and_says_so(self) -> None:
        profile = baker.make(User).profile
        account = GooglePhotosAccount(
            profile=profile,
            access_token="a",
            refresh_token="r",
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        gateway = GooglePhotosGateway(account=account, session=mock.MagicMock())
        page = {
            "mediaItems": [
                {"id": "a", "mediaFile": {"baseUrl": "https://x/a", "mimeType": "image/jpeg", "filename": "a.jpg"}}
            ],
            "nextPageToken": "more",
        }
        gateway.session.get.return_value = _json_response(page)
        gateway.session.get.return_value.ok = True

        with (
            mock.patch.object(google_photos, "_MAX_PICKER_PAGES", 3),
            self.assertLogs(google_photos.logger, "WARNING") as logs,
        ):
            items = gateway.list_session_media_items("sess")

        self.assertEqual(gateway.session.get.call_count, 3)
        self.assertEqual(len(items), 3)
        self.assertIn("3 pages", "\n".join(logs.output))

    def test_a_listing_that_ends_within_the_cap_is_whole_and_silent(self) -> None:
        profile = baker.make(User).profile
        account = GooglePhotosAccount(
            profile=profile,
            access_token="a",
            refresh_token="r",
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        gateway = GooglePhotosGateway(account=account, session=mock.MagicMock())
        last = _json_response({"mediaItems": [{"id": "z", "mediaFile": {"baseUrl": "https://x/z"}}]})
        last.ok = True
        gateway.session.get.return_value = last

        with (
            mock.patch.object(google_photos, "_MAX_PICKER_PAGES", 3),
            self.assertNoLogs(google_photos.logger, "WARNING"),
        ):
            self.assertEqual([item.id for item in gateway.list_session_media_items("sess")], ["z"])
