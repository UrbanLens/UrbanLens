"""Small grid thumbnails are written beside the stored original, not instead of it."""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import write_image_thumbnail


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
