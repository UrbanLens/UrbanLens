"""The backfill that scrubs photos stored before EXIF was stripped on upload.

Uploads stopped carrying EXIF, but the files already in storage are the ones that
have had time to reach a wiki. This covers the two things the command has to get
right: the block leaves the file, and the values survive on the row for any photo
that never recorded them.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image

_MAKE_TAG = 0x010F
_GPS_IFD = 0x8825


def _jpeg_with_exif() -> bytes:
    img = PILImage.new("RGB", (320, 240), (10, 20, 30))
    exif = PILImage.Exif()
    exif[_MAKE_TAG] = "ACME Cameras"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _jpeg_without_exif() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (320, 240), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


class StripExifBackfillTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_exif_backfill_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _stored_image(self, data: bytes, name: str = "old.jpg") -> Image:
        image = baker.make(Image, image=None, exif_data=None)
        image.image.save(name, ContentFile(data), save=True)
        return image

    def _read_back(self, image: Image) -> PILImage.Image:
        image.refresh_from_db()
        with image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def test_the_block_leaves_the_file(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos")

        out = self._read_back(image)
        self.assertIsNone(out.getexif().get(_MAKE_TAG), "the camera make is still in the stored file")
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD), "the GPS IFD is still in the stored file")

    def test_the_values_are_kept_on_the_row(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos")

        image.refresh_from_db()
        self.assertIsNotNone(image.exif_data, "the provenance was destroyed rather than moved")
        self.assertEqual(image.exif_data.get("Make"), "ACME Cameras")

    def test_an_already_recorded_row_is_not_overwritten(self) -> None:
        """A photo whose EXIF was captured on upload keeps what it captured."""
        image = self._stored_image(_jpeg_with_exif())
        Image.objects.filter(pk=image.pk).update(exif_data={"Make": "recorded earlier"})

        call_command("strip_exif_from_stored_photos")

        image.refresh_from_db()
        self.assertEqual(image.exif_data.get("Make"), "recorded earlier")

    def test_a_dry_run_changes_nothing(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos", "--dry-run")

        out = self._read_back(image)
        self.assertEqual(out.getexif().get(_MAKE_TAG), "ACME Cameras", "--dry-run rewrote the file")
        image.refresh_from_db()
        self.assertIsNone(image.exif_data, "--dry-run wrote to the row")

    def test_a_clean_photo_is_left_alone(self) -> None:
        """No block to remove means no rewrite, so the stored name does not change."""
        image = self._stored_image(_jpeg_without_exif(), name="clean.jpg")
        original_name = image.image.name

        call_command("strip_exif_from_stored_photos")

        image.refresh_from_db()
        self.assertEqual(image.image.name, original_name)
