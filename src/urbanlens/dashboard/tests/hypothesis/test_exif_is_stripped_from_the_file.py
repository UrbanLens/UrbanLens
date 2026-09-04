"""EXIF is recorded in the database and removed from the file we serve.

A photo contributed to a wiki is served to everyone who can reach that wiki, and
until now it carried its whole EXIF block with it - camera make, model and serial,
lens, software, timestamps, and GPS unless the uploader had found the location
opt-out. The block was re-attached deliberately on save, so this was not a leak
through a gap; the pipeline put it back.

Orientation is the reason it was kept: browsers rotate from tag 274, and this
pipeline had no ``exif_transpose``, so dropping the block made a photo render
ninety degrees wrong. Stripping therefore has to bake the rotation into the
pixels first - which is what TIFF already did, since Pillow rotates it on load.

The case that matters most is a photo needing neither a resize nor a conversion:
the rewrite used to be skipped entirely for those, so the original file survived
untouched with everything in it.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import _GPS_IFD_TAG, downscale_stored_image

_ORIENTATION_TAG = 0x0112
_ROTATE_90 = 6
_MAKE_TAG = 0x010F
_MODEL_TAG = 0x0110

#: Small enough that no resize is wanted, so the "nothing to do" path is exercised.
_SMALL = (320, 160)
_LARGE = (2400, 1200)


def _image_with_exif(fmt: str, size: tuple[int, int], *, with_gps: bool = True, orientation: bool = True) -> bytes:
    """A real image carrying identifying EXIF, and optionally GPS/orientation."""
    img = PILImage.new("RGB", size, (10, 20, 30))
    exif = img.getexif()
    exif[_MAKE_TAG] = "ACME Cameras"
    exif[_MODEL_TAG] = "Nosy 9000"
    if orientation:
        exif[_ORIENTATION_TAG] = _ROTATE_90
    if with_gps:
        gps = exif.get_ifd(_GPS_IFD_TAG)
        gps[1] = "N"
        gps[2] = (IFDRational(42), IFDRational(39), IFDRational(0))
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, exif=exif.tobytes())
    return buffer.getvalue()


class ExifStrippedFromStoredFileTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_exif_strip_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _stored(self, fmt: str, ext: str, size: tuple[int, int], **kwargs) -> PILImage.Image:
        image = baker.make(Image, image=None)
        image.image.save(f"e.{ext}", ContentFile(_image_with_exif(fmt, size, **kwargs)), save=True)

        downscale_stored_image(image, max_dimension=800, convert_webp=False)

        with image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def test_the_fixture_really_carries_exif(self) -> None:
        """Otherwise every assertion below passes on an empty block."""
        back = PILImage.open(io.BytesIO(_image_with_exif("JPEG", _SMALL)))

        self.assertEqual(back.getexif().get(_MAKE_TAG), "ACME Cameras")
        self.assertTrue(back.getexif().get_ifd(_GPS_IFD_TAG))

    def test_a_photo_needing_no_resize_still_loses_its_exif(self) -> None:
        """The path that used to return early and leave the original in place."""
        out = self._stored("JPEG", "jpg", _SMALL)

        self.assertIsNone(out.getexif().get(_MAKE_TAG), "the camera make survived into the served file")
        self.assertIsNone(out.getexif().get(_MODEL_TAG), "the camera model survived into the served file")

    def test_gps_is_gone_without_the_uploader_opting_in(self) -> None:
        """Stripping is unconditional now - it is not a setting you have to find."""
        out = self._stored("JPEG", "jpg", _SMALL)

        self.assertFalse(out.getexif().get_ifd(_GPS_IFD_TAG), "GPS coordinates were served inside the photo")

    def test_a_resized_photo_loses_its_exif_too(self) -> None:
        out = self._stored("JPEG", "jpg", _LARGE)

        self.assertIsNone(out.getexif().get(_MAKE_TAG))
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD_TAG))

    def test_the_rotation_is_baked_into_the_pixels_before_stripping(self) -> None:
        """Dropping tag 274 without rotating renders the photo ninety degrees wrong."""
        out = self._stored("JPEG", "jpg", _LARGE)

        self.assertGreater(
            out.height, out.width, "a rotate-90 source came back landscape - the orientation was simply lost"
        )

    def test_png_loses_its_exif_as_well(self) -> None:
        """PNG carries EXIF in an eXIf chunk, which Pillow writes on save."""
        out = self._stored("PNG", "png", _SMALL)

        self.assertIsNone(out.getexif().get(_MAKE_TAG))

    def test_an_unrotated_photo_is_left_the_right_way_up(self) -> None:
        """exif_transpose must be a no-op when there is nothing to correct."""
        out = self._stored("JPEG", "jpg", _LARGE, orientation=False)

        self.assertGreater(out.width, out.height, "a landscape source with no orientation tag came back rotated")
