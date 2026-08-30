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

    def test_regenerating_keeps_a_thumbnail_another_row_still_uses(self) -> None:
        """A deduplicated upload shares its thumbnail; force must not blank it.

        ``attach_deduped_copy`` gives the new row the earlier row's stored file
        *and* thumbnail name rather than copying either, so replacing one row's
        preview used to delete a file the other row was still pointing at.
        """
        shared = baker.make_recipe("dashboard.image")
        shared.image.save("shot.jpg", ContentFile(_jpeg()), save=True)
        write_image_thumbnail(shared)
        shared.save(update_fields=["thumbnail"])
        shared_thumb = shared.thumbnail.name
        storage = shared.thumbnail.storage

        # The deduped sibling: same stored file, same thumbnail, its own row.
        sibling = baker.make_recipe("dashboard.image")
        sibling.image.name = shared.image.name
        sibling.thumbnail.name = shared_thumb
        sibling.save(update_fields=["image", "thumbnail"])

        # Regenerate the first row's preview at a different size, so the new
        # name differs and the old one becomes a deletion candidate.
        self.assertTrue(write_image_thumbnail(shared, max_dimension=64, force=True))
        shared.save(update_fields=["thumbnail"])

        self.assertNotEqual(shared.thumbnail.name, shared_thumb)
        self.assertTrue(storage.exists(shared_thumb), "sibling row's thumbnail was deleted")

        # With the sibling gone, nothing references it and the next regeneration
        # is free to clean it up - otherwise this would leak a file per rewrite.
        sibling.delete()
        stale = shared.thumbnail.name
        self.assertTrue(write_image_thumbnail(shared, max_dimension=48, force=True))
        shared.save(update_fields=["thumbnail"])
        self.assertFalse(storage.exists(stale), "unshared thumbnail should be cleaned up")


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
