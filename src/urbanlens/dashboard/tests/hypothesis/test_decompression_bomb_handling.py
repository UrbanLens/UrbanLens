"""A decompression bomb must not take the photo-processing task down with it.

Pillow refuses to decode an image above `Image.MAX_IMAGE_PIXELS` (89 MP by
default), which is what stops the memory exhaustion. It signals that with
`DecompressionBombError` - and unlike the rest of Pillow's failures, that
inherits straight from `Exception`, **not** from `OSError`:

    UnidentifiedImageError -> OSError -> Exception     (caught)
    DecompressionBombError -> Exception                (was not)

`_process_photo_upload` caught `(OSError, ValueError)` around the downscale, so
a bomb escaped and failed the whole Celery task: the upload stayed stored but
unprocessed - no checksum, no EXIF, no downscale - and the failure surfaced as an
unhandled task exception rather than the logged warning every other
unprocessable image gets.

Tested by lowering `MAX_IMAGE_PIXELS` rather than building a real 89-megapixel
file, which would need gigabytes of memory to construct. That exercises the same
error from the same call, which is the part that was unhandled.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.tasks import _process_photo_upload


def _jpeg(size: tuple[int, int] = (1200, 900)) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, (90, 40, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


class DecompressionBombHandlingTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_bomb_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.image = baker.make(Image, image=None, profile=baker.make("auth.User").profile)
        self.image.image.save("bomb.jpg", ContentFile(_jpeg()), save=True)

    def test_the_fixture_really_trips_pillows_guard(self) -> None:
        """Without this the test below could pass because nothing raised at all."""
        with patch.object(PILImage, "MAX_IMAGE_PIXELS", 16), self.assertRaises(PILImage.DecompressionBombError), self.image.image.open("rb") as handle:
            PILImage.open(io.BytesIO(handle.read())).load()

    def test_a_bomb_does_not_raise_out_of_the_upload_pipeline(self) -> None:
        with patch.object(PILImage, "MAX_IMAGE_PIXELS", 16):
            result = _process_photo_upload(self.image, self.image.pk, strip_location=False)

        self.assertIsNotNone(result, "the pipeline should complete, not abort, on an undecodable image")

    def test_the_failure_is_logged_rather_than_swallowed(self) -> None:
        """Degrading quietly is not the same as degrading silently."""
        with patch.object(PILImage, "MAX_IMAGE_PIXELS", 16), self.assertLogs("urbanlens.dashboard.tasks", level="WARNING") as logs:
            _process_photo_upload(self.image, self.image.pk, strip_location=False)

        self.assertTrue(any("Downscaling failed" in line for line in logs.output), logs.output)

    def test_an_ordinary_image_is_unaffected(self) -> None:
        """The guard must not change the normal path."""
        result = _process_photo_upload(self.image, self.image.pk, strip_location=False)

        self.assertIsNotNone(result)
