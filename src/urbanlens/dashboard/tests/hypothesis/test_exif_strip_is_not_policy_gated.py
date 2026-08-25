"""Stripping EXIF does not depend on the uploader's downscale policy.

``_process_photo_upload`` only called ``downscale_stored_image`` when there was a
resize, a WebP conversion, a location opt-out, or a HEIC to transcode. That was
right while the function existed to resize; it is wrong now that the same
function is what removes EXIF, because a profile whose policy is "no cap, no
conversion" never reached it - and that policy is what a downscale-exempt
subscriber gets. The people paying us kept the leak.

Driven through ``_process_photo_upload`` rather than the queryset beneath it, so
the gate itself is what is under test.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.tasks import _process_photo_upload

_MAKE_TAG = 0x010F
_GPS_IFD = 0x8825


def _jpeg_with_exif() -> bytes:
    """Small enough that no policy would resize it anyway."""
    img = PILImage.new("RGB", (320, 240), (10, 20, 30))
    exif = PILImage.Exif()
    exif[_MAKE_TAG] = "ACME Cameras"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


class ExifStripIgnoresDownscalePolicyTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_exif_policy_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.profile = baker.make(User).profile
        self.image = baker.make(Image, image=None, profile=self.profile)
        self.image.image.save("p.jpg", ContentFile(_jpeg_with_exif()), save=True)

    def _run_with_policy(self, policy: tuple[int | None, bool], *, strip_location: bool = False) -> PILImage.Image:
        with patch("urbanlens.dashboard.services.media.storage.get_downscale_policy", return_value=policy):
            result = _process_photo_upload(self.image, self.image.pk, strip_location)
        if result is not None and result.update_fields.get("image"):
            self.image.image.name = result.update_fields["image"]
            self.image.save(update_fields=["image"])
        with self.image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def test_no_cap_and_no_conversion_still_strips_exif(self) -> None:
        """The exempt-subscriber policy - previously the gate that skipped everything."""
        out = self._run_with_policy((None, False))

        self.assertIsNone(out.getexif().get(_MAKE_TAG), "a downscale-exempt profile kept its EXIF")
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD), "a downscale-exempt profile kept its GPS")

    def test_the_exif_is_still_recorded_on_the_row(self) -> None:
        """Stripped from the file, kept in the database - the point of the exercise."""
        with patch("urbanlens.dashboard.services.media.storage.get_downscale_policy", return_value=(None, False)):
            result = _process_photo_upload(self.image, self.image.pk, False)

        assert result is not None
        self.assertEqual(result.update_fields.get("exif_data", {}).get("Make"), "ACME Cameras")

    def test_a_capped_policy_strips_it_too(self) -> None:
        """The path that already worked, kept as a control."""
        out = self._run_with_policy((800, False))

        self.assertIsNone(out.getexif().get(_MAKE_TAG))
