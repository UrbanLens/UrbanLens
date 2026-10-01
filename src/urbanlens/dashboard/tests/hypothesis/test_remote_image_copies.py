"""Third-party images are shown from this site's own copy, made once and kept after the provider drops them (P165).

Jess, 2026-09-30: download and keep forever, with a record of where each came from.
"""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.media.previews import gallery_thumb_urls, gallery_urls
from urbanlens.dashboard.services.media.remote_copies import (
    FIRST_RETRY_DELAY,
    RemoteImage,
    copy_url,
    copy_urls,
    pending_marker,
    url_digest,
)
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-remote-copies-")
_FETCH = "urbanlens.dashboard.services.media.previews.fetch_remote_source"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _jpeg(size: tuple[int, int] = (40, 30)) -> bytes:
    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new("RGB", size, "red").save(buffer, format="JPEG")
    return buffer.getvalue()


def _item(url: str, thumb_url: str = "", content_type: str = "") -> MediaItem:
    return MediaItem(
        url=url,
        thumb_url=thumb_url,
        caption="",
        source="Test",
        page_url="https://provider.test/page",
        content_type=content_type,
    )


class CopyRecordTests(TestCase):
    def test_a_copy_records_where_it_came_from(self) -> None:
        url = copy_url("https://provider.test/a.jpg", provider="wikimedia", page_url="https://provider.test/page/a")

        copy = RemoteImageCopy.objects.get()
        self.assertEqual(url, reverse("media.remote_copy", args=[copy.url_digest]))
        self.assertEqual(
            (copy.source_url, copy.provider, copy.page_url),
            ("https://provider.test/a.jpg", "wikimedia", "https://provider.test/page/a"),
        )

    def test_the_same_image_shares_one_copy(self) -> None:
        first = copy_url("https://provider.test/a.jpg", provider="wikimedia")
        second = copy_url("https://provider.test/a.jpg", provider="flickr")

        self.assertEqual(first, second)
        self.assertEqual(RemoteImageCopy.objects.get().provider, "wikimedia")

    def test_only_remote_addresses_are_copied(self) -> None:
        copies = copy_urls(
            [RemoteImage("/dashboard/cris/attachment/1/", "cris"), RemoteImage("data:image/png;base64,AA", "x")]
        )

        self.assertEqual(copies, {})
        self.assertFalse(RemoteImageCopy.objects.exists())
        self.assertEqual(copy_url("/local.jpg", provider="x"), "/local.jpg")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class CopyEndpointTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = f"https://provider.test/{self.id().rsplit('.', 1)[-1]}.jpg"
        self.url = copy_url(self.source, provider="test")
        self.copy = RemoteImageCopy.objects.get()
        self.addCleanup(cache.delete, pending_marker(self.copy.url_digest))

    def _first_request(self) -> None:
        """The page's request: it starts the download and answers at once, whatever the provider's speed."""
        from urbanlens.dashboard.tasks import fetch_remote_image_copy

        with patch(_FETCH, side_effect=AssertionError("downloaded while the page waited")), patch(_ENQUEUE) as enqueue:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.has_header("Retry-After"))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[:2], (fetch_remote_image_copy, self.copy.pk))

    def _download(self, body: bytes | None) -> MagicMock:
        """The worker's download: the provider's bytes, staged for the sandbox render it queues."""
        from urbanlens.dashboard.tasks import fetch_remote_image_copy

        with (
            patch(_FETCH, return_value=(body, "image/jpeg") if body is not None else None) as fetch,
            patch(_ENQUEUE) as enqueue,
        ):
            fetch_remote_image_copy(self.copy.pk)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, (self.source,))
        return enqueue

    def _render(self, enqueue: MagicMock) -> bool:
        from urbanlens.dashboard.tasks import render_remote_image_copy

        _task, copy_id, descriptor = enqueue.call_args.args
        return render_remote_image_copy(copy_id, descriptor)

    def _make(self, body: bytes) -> bool:
        self._first_request()
        return self._render(self._download(body))

    def test_a_digest_this_site_never_issued_is_refused_without_fetching(self) -> None:
        with patch(_FETCH) as fetch, patch(_ENQUEUE) as enqueue:
            response = self.client.get(reverse("media.remote_copy", args=[url_digest("https://evil.test/internal")]))

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()
        enqueue.assert_not_called()

    def test_a_slow_provider_gets_longer_than_a_web_request_would_wait(self) -> None:
        """P180: the USGS export took 18 s and 31 s, and the in-request fetch gave up at 20."""
        from urbanlens.dashboard.services.media.remote_copies import DOWNLOAD_TIMEOUT_SECONDS
        from urbanlens.dashboard.tasks import fetch_remote_image_copy

        with patch(_FETCH, return_value=(_jpeg(), "image/jpeg")) as fetch, patch(_ENQUEUE):
            fetch_remote_image_copy(self.copy.pk)

        self.assertEqual(fetch.call_args.kwargs["timeout"], DOWNLOAD_TIMEOUT_SECONDS)
        self.assertGreaterEqual(DOWNLOAD_TIMEOUT_SECONDS, 60)

    def test_requests_while_the_copy_is_being_made_do_not_start_it_again(self) -> None:
        self._first_request()
        with patch(_ENQUEUE) as enqueue:
            for _ in range(3):
                self.assertEqual(self.client.get(self.url).status_code, 503)

        enqueue.assert_not_called()

    def test_once_made_the_copy_is_served_without_the_provider(self) -> None:
        self.assertTrue(self._make(_jpeg()))

        with patch(_FETCH, side_effect=AssertionError("fetched the provider again")), patch(_ENQUEUE) as enqueue:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        enqueue.assert_not_called()
        self.assertEqual(b"".join(response.streaming_content)[:2], b"\xff\xd8")
        self.copy.refresh_from_db()
        self.assertTrue(self.copy.file.name.startswith("remote_copies/"))
        self.assertEqual(
            (self.copy.content_type, self.copy.file_size > 0, len(self.copy.checksum)), ("image/jpeg", True, 64)
        )
        self.assertIsNotNone(self.copy.fetched_at)

    def test_the_copy_is_re_encoded_and_bounded(self) -> None:
        from PIL import Image as PILImage

        from urbanlens.dashboard.services.media.remote_copies import REMOTE_COPY_MAX_DIMENSION

        self._make(_jpeg((REMOTE_COPY_MAX_DIMENSION * 2, 50)))

        self.copy.refresh_from_db()
        with self.copy.file.open("rb") as stored, PILImage.open(stored) as image:
            self.assertLessEqual(max(image.size), REMOTE_COPY_MAX_DIMENSION)

    def test_a_failed_download_is_not_retried_until_its_backoff_runs_out(self) -> None:
        self._first_request()
        self._download(None).assert_not_called()
        with patch(_ENQUEUE) as enqueue:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        enqueue.assert_not_called()

        RemoteImageCopy.objects.filter(pk=self.copy.pk).update(
            last_failed_at=timezone.now() - FIRST_RETRY_DELAY - timedelta(minutes=1)
        )
        self._first_request()
        self._download(None)
        self.assertEqual(RemoteImageCopy.objects.get(pk=self.copy.pk).failed_attempts, 2)

    def test_a_caller_over_the_download_rate_is_told_to_retry_without_counting_a_failure(self) -> None:
        with (
            patch("urbanlens.dashboard.services.security.throttle.allow", return_value=False),
            patch(_ENQUEUE) as enqueue,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.has_header("Retry-After"))
        enqueue.assert_not_called()
        self.assertEqual(RemoteImageCopy.objects.get(pk=self.copy.pk).failed_attempts, 0)
        self._first_request()

    def test_bytes_that_are_not_an_image_are_a_failure_not_a_copy(self) -> None:
        self.assertFalse(self._make(b"<html>not an image</html>"))

        self.copy.refresh_from_db()
        self.assertEqual((self.copy.file.name, self.copy.failed_attempts), ("", 1))
        with patch(_ENQUEUE) as enqueue:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        enqueue.assert_not_called()


class GalleryUrlTests(TestCase):
    def test_a_provider_thumbnail_becomes_a_copy(self) -> None:
        (urls,) = gallery_urls(
            [_item("https://provider.test/full.jpg", "https://provider.test/thumb.jpg")], provider="p"
        )

        self.assertEqual(urls.thumb, reverse("media.remote_copy", args=[url_digest("https://provider.test/thumb.jpg")]))
        self.assertEqual(urls.view, reverse("media.remote_copy", args=[url_digest("https://provider.test/full.jpg")]))
        self.assertEqual(
            set(RemoteImageCopy.objects.values_list("page_url", flat=True)), {"https://provider.test/page"}
        )

    def test_a_tiff_thumbnail_is_copied_too(self) -> None:
        """Several archives serve the original file as the "thumbnail"; the copy's re-encode makes it viewable."""
        (thumb,) = gallery_thumb_urls(
            [_item("https://provider.test/full.tif", "https://provider.test/thumb.tif")], provider="p"
        )

        self.assertEqual(thumb, reverse("media.remote_copy", args=[url_digest("https://provider.test/thumb.tif")]))

    def test_an_in_app_document_is_previewed_in_place(self) -> None:
        (urls,) = gallery_urls([_item("/dashboard/cris/attachment/r1/2/", "", "application/pdf")], provider="cris")

        self.assertEqual((urls.thumb, urls.view), ("/dashboard/cris/attachment/r1/2/?preview=1", ""))
        self.assertFalse(RemoteImageCopy.objects.exists())

    def test_an_in_app_documents_own_query_is_kept(self) -> None:
        (thumb,) = gallery_thumb_urls([_item("/x/?a=b", "", "image/tiff")], provider="cris")

        self.assertEqual(thumb, "/x/?a=b&preview=1")

    def test_an_item_with_nothing_to_show_gets_no_picture(self) -> None:
        (urls,) = gallery_urls([_item("https://provider.test/record.txt")], provider="p")

        self.assertEqual((urls.thumb, urls.view), ("", ""))
        self.assertFalse(RemoteImageCopy.objects.exists())

    def test_thumbnails_alone_do_not_record_full_size_copies(self) -> None:
        gallery_thumb_urls([_item("https://provider.test/full.jpg", "https://provider.test/thumb.jpg")], provider="p")

        self.assertEqual(
            list(RemoteImageCopy.objects.values_list("source_url", flat=True)), ["https://provider.test/thumb.jpg"]
        )


class GalleryPageTests(TestCase):
    """The rendered gallery names no provider image as something for the browser to load."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        LocationCache.set(self.pin.location, "stub_gallery", {}, query_key="")
        self.client.force_login(self.pin.profile.user)

    def test_tiles_and_the_lightbox_use_copies(self) -> None:
        panel = MagicMock(spec=GalleryMediaSource)
        panel.required_feature = None
        panel.cache_source = "stub_gallery"
        panel.gate.return_value = True
        panel.media_is_ready.return_value = True
        panel.media_items.return_value = [_item("https://provider.test/full.jpg", "https://provider.test/thumb.jpg")]

        with patch("urbanlens.dashboard.services.pins.external_data.get_panel_source", return_value=panel):
            response = self.client.get(reverse("pin.media", args=[self.pin.slug, "stub_gallery"]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "provider.test/thumb.jpg")
        self.assertContains(
            response, f'src="{reverse("media.remote_copy", args=[url_digest("https://provider.test/thumb.jpg")])}"'
        )
        self.assertContains(
            response,
            f'data-media-view-url="{reverse("media.remote_copy", args=[url_digest("https://provider.test/full.jpg")])}"',
        )
        self.assertContains(response, 'data-media-url="https://provider.test/full.jpg"')

    def test_the_wikipedia_panel_uses_a_copy(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(
            self.pin.location,
            "wikipedia",
            {
                "title": "Old Mill",
                "url": "https://en.wikipedia.org/wiki/Old_Mill",
                "thumbnail": "https://upload.wikimedia.org/m.jpg",
            },
            query_key="",
        )

        response = self.client.get(reverse("pin.wikipedia", args=[self.pin.slug]))

        self.assertNotContains(response, "upload.wikimedia.org")
        self.assertContains(
            response, reverse("media.remote_copy", args=[url_digest("https://upload.wikimedia.org/m.jpg")])
        )
        self.assertEqual(RemoteImageCopy.objects.get().page_url, "https://en.wikipedia.org/wiki/Old_Mill")

    def test_the_pins_own_photo_tile_shows_the_photo_not_an_icon(self) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile

        from urbanlens.dashboard.models.images.model import Image

        photo = Image.objects.create(
            image=SimpleUploadedFile("mine.jpg", b"bytes", content_type="image/jpeg"),
            pin=self.pin,
            profile=self.pin.profile,
            location=self.pin.location,
        )

        response = self.client.get(reverse("pin.media", args=[self.pin.slug, "photos"]))

        self.assertContains(response, f'<img class="media-item-thumb" src="{photo.thumb_url}"')
