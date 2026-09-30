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
_FETCH = "urbanlens.dashboard.controllers.remote_copies.fetch_remote_source"
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

    def _first_request(self, body: bytes) -> MagicMock:
        with patch(_FETCH, return_value=(body, "image/jpeg")) as fetch, patch(_ENQUEUE) as enqueue:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.has_header("Retry-After"))
        fetch.assert_called_once_with(self.source, max_bytes=fetch.call_args.kwargs["max_bytes"])
        return enqueue

    def _render(self, enqueue: MagicMock) -> bool:
        from urbanlens.dashboard.tasks import render_remote_image_copy

        _task, copy_id, descriptor = enqueue.call_args.args
        return render_remote_image_copy(copy_id, descriptor)

    def test_a_digest_this_site_never_issued_is_refused_without_fetching(self) -> None:
        with patch(_FETCH) as fetch:
            response = self.client.get(reverse("media.remote_copy", args=[url_digest("https://evil.test/internal")]))

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_requests_while_the_copy_is_being_made_do_not_fetch_again(self) -> None:
        self._first_request(_jpeg())
        with patch(_FETCH) as fetch:
            for _ in range(3):
                self.assertEqual(self.client.get(self.url).status_code, 503)

        fetch.assert_not_called()

    def test_once_made_the_copy_is_served_without_the_provider(self) -> None:
        self.assertTrue(self._render(self._first_request(_jpeg())))

        with patch(_FETCH, side_effect=AssertionError("fetched the provider again")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
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

        self._render(self._first_request(_jpeg((REMOTE_COPY_MAX_DIMENSION * 2, 50))))

        self.copy.refresh_from_db()
        with self.copy.file.open("rb") as stored, PILImage.open(stored) as image:
            self.assertLessEqual(max(image.size), REMOTE_COPY_MAX_DIMENSION)

    def test_a_failed_download_is_not_retried_until_its_backoff_runs_out(self) -> None:
        with patch(_FETCH, return_value=None):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        with patch(_FETCH) as fetch:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        fetch.assert_not_called()

        RemoteImageCopy.objects.filter(pk=self.copy.pk).update(
            last_failed_at=timezone.now() - FIRST_RETRY_DELAY - timedelta(minutes=1)
        )
        with patch(_FETCH, return_value=None) as fetch:
            self.client.get(self.url)
        fetch.assert_called_once()
        self.assertEqual(RemoteImageCopy.objects.get(pk=self.copy.pk).failed_attempts, 2)

    def test_a_request_over_the_download_slots_is_told_to_retry_without_counting_a_failure(self) -> None:
        from urbanlens.dashboard.services.apis.request_upstreams import RemoteImageCopyUpstream

        semaphore = RemoteImageCopyUpstream.semaphore()
        held = 0
        while semaphore.acquire(blocking=False):
            held += 1
        try:
            with patch(_FETCH) as fetch:
                response = self.client.get(self.url)
        finally:
            for _ in range(held):
                semaphore.release()

        self.assertEqual(response.status_code, 503)
        fetch.assert_not_called()
        self.assertEqual(RemoteImageCopy.objects.get(pk=self.copy.pk).failed_attempts, 0)
        self._first_request(_jpeg())

    def test_bytes_that_are_not_an_image_are_a_failure_not_a_copy(self) -> None:
        self.assertFalse(self._render(self._first_request(b"<html>not an image</html>")))

        self.copy.refresh_from_db()
        self.assertEqual((self.copy.file.name, self.copy.failed_attempts), ("", 1))
        with patch(_FETCH) as fetch:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        fetch.assert_not_called()


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
