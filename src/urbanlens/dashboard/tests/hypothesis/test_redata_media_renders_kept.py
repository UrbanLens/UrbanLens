"""A REData-proxied file a browser cannot show (a CRIS PDF, a TIFF) is rendered once and kept, at a tile's size for tiles.

P189: a gallery tile loaded the lightbox's 1200 px render of a document's first page, and the render lived an hour in
the cache, after which the next view downloaded the document from REData and decoded it again.
"""

from __future__ import annotations

import io
from unittest import mock

from django.conf import settings
from django.core.cache import cache, caches
from django.core.files.storage import default_storage
from django.test import Client
from django.urls import reverse
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
from urbanlens.dashboard.services.media.previews import gallery_urls
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_ROUTE = ("pin.cris.attachment", ("res-1", 5))


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), (120, 30, 200)).save(out, format="PNG")
    return out.getvalue()


def _tiff(width: int, height: int) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), (10, 140, 60)).save(out, format="TIFF")
    return out.getvalue()


def _noise_tiff(width: int, height: int) -> bytes:
    """A TIFF too large for the proxied-bytes cache, which incompressible pixels guarantee."""
    out = io.BytesIO()
    PILImage.frombytes("RGB", (width, height), bytes((i * 7919) % 251 for i in range(width * height * 3))).save(
        out, format="TIFF"
    )
    return out.getvalue()


def _run_enqueued(task, *args, **_enqueue_options):
    """Stands in for the broker: the sandbox task runs as soon as it is queued."""
    task(*args)
    return mock.Mock()


class _Base(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        for store in (cache, caches[settings.PROXIED_BYTES_CACHE]):
            store.clear()
            self.addCleanup(store.clear)
        self.url = reverse(_ROUTE[0], args=_ROUTE[1])
        self.downloads = 0

    def _source(self, body: bytes, content_type: str):
        def download(_gateway, *_args):
            self.downloads += 1
            return body, content_type

        return mock.patch.object(RedataGateway, "download_cultural_resource_attachment", download)

    def _get(self, preview: str):
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", side_effect=_run_enqueued):
            return Client().get(self.url, {"preview": preview})

    def _image(self, response) -> PILImage.Image:
        body = b"".join(response.streaming_content) if response.streaming else response.content
        return PILImage.open(io.BytesIO(body))


class TileRenditionTests(_Base):
    def test_a_tile_gets_a_tile_sized_rendering_not_the_lightboxs(self) -> None:
        with self._source(_tiff(2400, 1800), "image/tiff"):
            first = self._get("thumb")
            self.assertIn(first.status_code, (200, 503))
            response = self._get("thumb")
        self.assertEqual(response.status_code, 200)
        image = self._image(response)
        self.assertLessEqual(max(image.size), 400, f"a tile was sent {image.size}")

    def test_the_lightbox_still_gets_the_large_rendering(self) -> None:
        with self._source(_tiff(2400, 1800), "image/tiff"):
            self._get("1")
            response = self._get("1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(max(self._image(response).size), 1200)

    def test_a_browser_viewable_original_is_still_shrunk_for_a_tile(self) -> None:
        with self._source(_png(3000, 2000), "image/png"):
            self._get("thumb")
            response = self._get("thumb")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(max(self._image(response).size), 400)

    def test_a_browser_viewable_original_is_served_as_it_is_for_the_lightbox(self) -> None:
        original = _png(3000, 2000)
        with self._source(original, "image/png"):
            response = self._get("1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._image(response).size, (3000, 2000))


class KeptRenditionTests(_Base):
    def test_a_rendering_outlives_the_cache(self) -> None:
        with self._source(_tiff(1600, 1200), "image/tiff"):
            self._get("thumb")
            self.assertEqual(self._get("thumb").status_code, 200)
            for store in (cache, caches[settings.PROXIED_BYTES_CACHE]):
                store.clear()
            response = self._get("thumb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.downloads, 1, "the document was downloaded from REData again once the cache emptied")

    def test_a_kept_rendering_is_served_as_immutable(self) -> None:
        with self._source(_tiff(800, 600), "image/tiff"):
            self._get("thumb")
            response = self._get("thumb")
        self.assertIn("immutable", response.get("Cache-Control", ""))

    def test_a_document_too_large_for_the_byte_cache_still_renders(self) -> None:
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_MAX_CACHED_BYTES

        large = _noise_tiff(1600, 1200)
        self.assertGreater(len(large), REDATA_MEDIA_MAX_CACHED_BYTES)
        with self._source(large, "image/tiff"):
            self._get("thumb")
            response = self._get("thumb")
        self.assertEqual(response.status_code, 200)

    def test_an_unrenderable_file_answers_404_without_rendering_again(self) -> None:
        with (
            self._source(b"not an image at all", "image/tiff"),
            mock.patch("urbanlens.dashboard.services.media.previews.render_preview", return_value=None) as render,
        ):
            self._get("thumb")
            self.assertEqual(self._get("thumb").status_code, 404)
            self.assertEqual(self._get("thumb").status_code, 404)
        self.assertEqual(render.call_count, 1)


class SecondRenderTests(TestCase):
    """A queue backed up past the queued mark's lifetime can render the same file twice, even at once."""

    key = "ul_cris_attachment_race_5"

    def _finish_twice(self) -> tuple[bytes, object]:
        from urbanlens.dashboard.models import ProxiedMediaRender
        from urbanlens.dashboard.services.media import proxied_renders

        first, second = _png(300, 200), _png(200, 300)
        proxied_renders.finish(self.key, proxied_renders.TILE, (first, "image/png"))
        folder = ProxiedMediaRender.objects.get(source_key=self.key).file.name.rsplit("/", 1)[0]
        self.addCleanup(
            lambda: [default_storage.delete(f"{folder}/{name}") for name in default_storage.listdir(folder)[1]]
        )
        proxied_renders.finish(self.key, proxied_renders.TILE, (second, "image/png"))
        [render] = ProxiedMediaRender.objects.filter(source_key=self.key)
        return first, render

    def _assert_first_kept_alone(self, first: bytes, render) -> None:
        with render.file.open("rb") as stored:
            self.assertEqual(stored.read(), first, "a kept rendering is served immutable, so it must not change")
        folder, name = render.file.name.rsplit("/", 1)
        self.assertEqual(default_storage.listdir(folder)[1], [name])

    def test_the_first_rendering_is_kept_and_the_second_leaves_no_file(self) -> None:
        self._assert_first_kept_alone(*self._finish_twice())

    def test_a_second_rendering_finishing_at_the_same_moment_neither_fails_nor_leaves_a_file(self) -> None:
        with mock.patch("django.db.models.query.QuerySet.exists", return_value=False):
            first, render = self._finish_twice()
        self._assert_first_kept_alone(first, render)


class TileUrlTests(TestCase):
    def test_a_gallery_tile_for_an_in_app_document_asks_for_the_tile_rendition(self) -> None:
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        url = reverse(_ROUTE[0], args=_ROUTE[1])
        [urls] = gallery_urls(
            [MediaItem(url=url, thumb_url="", caption="", source="CRIS", content_type="image/tiff")], provider="cris"
        )
        self.assertEqual(urls.thumb, f"{url}?preview=thumb")
