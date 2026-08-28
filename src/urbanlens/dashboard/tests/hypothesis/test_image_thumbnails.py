"""Small grid thumbnails are written beside the stored original, not instead of it."""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.media.images import photos_missing_thumbnails, write_image_thumbnail
from urbanlens.dashboard.tasks import backfill_image_thumbnails, generate_image_thumbnails


def _jpeg(size: tuple[int, int] = (800, 600)) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, (20, 80, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


class WriteImageThumbnailTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_thumb_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        (Path(self._media_root) / "pin_images" / "thumbs").mkdir(parents=True, exist_ok=True)
        self._settings = override_settings(MEDIA_ROOT=self._media_root)
        self._settings.enable()
        self.addCleanup(self._settings.disable)

    def test_writes_a_webp_preview_without_replacing_the_original(self) -> None:
        image = baker.make_recipe("dashboard.image")
        image.image.save("shot.jpg", ContentFile(_jpeg()), save=True)
        original_name = image.image.name

        written = write_image_thumbnail(image)
        image.save(update_fields=["thumbnail"])

        self.assertTrue(written)
        self.assertTrue(image.thumbnail)
        self.assertTrue(image.thumbnail.name.endswith(".webp"))
        self.assertEqual(image.image.name, original_name)
        self.assertEqual(image.thumb_url, image.thumbnail.url)

    def test_skips_a_row_that_already_has_a_thumbnail(self) -> None:
        image = baker.make_recipe("dashboard.image")
        image.image.save("shot.jpg", ContentFile(_jpeg()), save=True)
        write_image_thumbnail(image)
        image.save(update_fields=["thumbnail"])

        self.assertFalse(write_image_thumbnail(image))

    def test_skips_videos(self) -> None:
        image = baker.make_recipe("dashboard.image", media_type=MediaKind.VIDEO)
        image.image.save("clip.jpg", ContentFile(_jpeg()), save=True)

        self.assertFalse(write_image_thumbnail(image))
        self.assertFalse(image.thumbnail)


class ThumbnailBackfillTests(TestCase):
    """Existing photos get thumbnails from a periodic sweep, not from page views."""

    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_thumb_bf_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        (Path(self._media_root) / "pin_images" / "thumbs").mkdir(parents=True, exist_ok=True)
        self._settings = override_settings(MEDIA_ROOT=self._media_root)
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        cache.delete("image-thumbnail-backfill-cursor")
        self.addCleanup(lambda: cache.delete("image-thumbnail-backfill-cursor"))

    def _photo(self, *, with_thumb: bool = False) -> Image:
        image = baker.make_recipe("dashboard.image")
        image.image.save(f"shot-{image.pk}.jpg", ContentFile(_jpeg()), save=True)
        if with_thumb:
            write_image_thumbnail(image)
            image.save(update_fields=["thumbnail"])
        return image

    def test_photos_missing_thumbnails_skips_rows_that_already_have_one(self) -> None:
        ready = self._photo(with_thumb=True)
        pending = self._photo(with_thumb=False)

        ids = photos_missing_thumbnails()

        self.assertIn(pending.pk, ids)
        self.assertNotIn(ready.pk, ids)

    def test_photos_missing_thumbnails_skips_videos(self) -> None:
        video = baker.make_recipe("dashboard.image", media_type=MediaKind.VIDEO)
        video.image.save("clip.jpg", ContentFile(_jpeg()), save=True)

        self.assertNotIn(video.pk, photos_missing_thumbnails())

    def test_generate_image_thumbnails_writes_the_preview(self) -> None:
        image = self._photo(with_thumb=False)

        written = generate_image_thumbnails([image.pk])
        image.refresh_from_db()

        self.assertEqual(written, 1)
        self.assertTrue(image.thumbnail)

    def test_backfill_enqueues_a_batch_and_advances_the_cursor(self) -> None:
        with (
            patch("urbanlens.dashboard.services.media.images.photos_missing_thumbnails", side_effect=[[10, 11], [12], []]) as missing,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            self.assertEqual(backfill_image_thumbnails(limit=2), 2)
            self.assertEqual(enqueue.call_args.args[1], [10, 11])
            missing.assert_called_with(after_pk=0, limit=2)

            self.assertEqual(backfill_image_thumbnails(limit=2), 1)
            self.assertEqual(enqueue.call_args.args[1], [12])
            missing.assert_called_with(after_pk=11, limit=2)

            enqueue.reset_mock()
            self.assertEqual(backfill_image_thumbnails(limit=2), 0)
            enqueue.assert_not_called()
            missing.assert_called_with(after_pk=12, limit=2)

    def test_backfill_is_a_no_op_when_every_photo_already_has_a_thumb(self) -> None:
        with (
            patch("urbanlens.dashboard.services.media.images.photos_missing_thumbnails", return_value=[]),
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            queued = backfill_image_thumbnails()

        self.assertEqual(queued, 0)
        enqueue.assert_not_called()
