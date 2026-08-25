"""A downscale must not silently rotate a photo ninety degrees.

The pipeline now strips the whole EXIF block from every stored file, so tag 274
cannot be what keeps a photo upright any more. ``ImageOps.exif_transpose`` runs
first and spends the orientation on the pixels instead - which is what TIFF
always did, since Pillow rotates it on load.

So the expected outcome is the same for every format: the stored image arrives
already in its display orientation, and carries no tag. A landscape source with
a rotate-90 tag comes back portrait. These tests assert "displays correctly"
rather than any one mechanism, which is why they survived the change of
mechanism - only the docstring and the removed ``strip_gps`` argument moved.

The companion file is ``test_exif_is_stripped_from_the_file``, which covers the
removal itself; this one guards the thing removal could plausibly break.
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

#: EXIF tag 274. Value 6 means "rotate 90° clockwise to display".
_ORIENTATION_TAG = 0x0112
_ROTATE_90 = 6

#: Landscape on disk; portrait once the orientation is applied.
_SOURCE_SIZE = (2400, 1200)
_MAX_DIMENSION = 800


def _image_with_orientation(fmt: str, *, with_gps: bool = False) -> bytes:
    img = PILImage.new("RGB", _SOURCE_SIZE, (10, 20, 30))
    exif = img.getexif()
    exif[_ORIENTATION_TAG] = _ROTATE_90
    if with_gps:
        gps = exif.get_ifd(_GPS_IFD_TAG)
        gps[1] = "N"
        gps[2] = (IFDRational(42), IFDRational(39), IFDRational(0))
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, exif=exif.tobytes())
    return buffer.getvalue()


class OrientationPreservedTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_orientation_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _downscaled(self, fmt: str, ext: str, *, convert_webp: bool = False, with_gps: bool = False) -> PILImage.Image:
        image = baker.make(Image, image=None)
        image.image.save(f"o.{ext}", ContentFile(_image_with_orientation(fmt, with_gps=with_gps)), save=True)

        downscale_stored_image(image, max_dimension=_MAX_DIMENSION, convert_webp=convert_webp)

        with image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def _assert_displays_upright(self, out: PILImage.Image, label: str) -> None:
        """The pixels must carry the rotation, since the tag no longer survives."""
        self.assertGreater(out.height, out.width, f"{label}: orientation lost - the image now renders 90 degrees wrong")

    def test_fixture_actually_carries_an_orientation(self) -> None:
        """Without this the assertions below could pass on a tagless fixture."""
        for fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
            back = PILImage.open(io.BytesIO(_image_with_orientation(fmt)))
            self.assertEqual(back.getexif().get(_ORIENTATION_TAG), _ROTATE_90, fmt)

    def test_png_still_displays_upright(self) -> None:
        """PNG is the format this originally broke on."""
        self._assert_displays_upright(self._downscaled("PNG", "png"), "PNG")

    def test_jpeg_still_displays_upright(self) -> None:
        self._assert_displays_upright(self._downscaled("JPEG", "jpg"), "JPEG")

    def test_tiff_still_displays_upright(self) -> None:
        """TIFF drops the tag but arrives pre-rotated, which is equally correct."""
        out = self._downscaled("TIFF", "tif")
        self.assertGreater(out.height, out.width, "TIFF should come back portrait, i.e. already rotated")

    def test_webp_conversion_still_displays_upright(self) -> None:
        self._assert_displays_upright(self._downscaled("JPEG", "jpg", convert_webp=True), "JPEG->WEBP")

    def test_a_gps_tagged_photo_is_rotated_and_scrubbed(self) -> None:
        """The two things that happen to the block must not interfere."""
        out = self._downscaled("JPEG", "jpg", with_gps=True)

        self._assert_displays_upright(out, "JPEG with GPS")
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD_TAG), "GPS should be gone")
