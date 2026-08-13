"""A downscale must not silently rotate a photo ninety degrees.

Nothing in this pipeline calls `ImageOps.exif_transpose`, which is fine *provided*
the EXIF `Orientation` tag survives the re-encode - browsers rotate from it. The
save path preserved EXIF for JPEG/WEBP/TIFF/AVIF but not PNG, so a PNG carrying an
orientation kept its unrotated pixels and lost the tag that explained them. The
result renders ninety degrees wrong, permanently, with nothing logged.

The two acceptable outcomes are different per format, which is why these tests
assert "displays correctly" rather than one mechanism:

- **JPEG / WEBP / PNG** keep both the pixels and the tag.
- **TIFF** loses the tag, but Pillow rotates the pixels on load, so the stored
  image is already in its display orientation - the landscape source comes back
  portrait. Asserting the tag survived would fail on a correct result.

The GPS-strip case is covered too: that path rebuilds the EXIF block from
`getexif()` after deleting the GPS IFD, so it is a second place orientation could
be dropped.
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

    def _downscaled(self, fmt: str, ext: str, *, convert_webp: bool = False, strip_gps: bool = False) -> PILImage.Image:
        image = baker.make(Image, image=None)
        image.image.save(f"o.{ext}", ContentFile(_image_with_orientation(fmt, with_gps=strip_gps)), save=True)

        downscale_stored_image(image, max_dimension=_MAX_DIMENSION, convert_webp=convert_webp, strip_gps=strip_gps)

        with image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def _assert_displays_upright(self, out: PILImage.Image, label: str) -> None:
        """Either the tag survived, or the pixels were already rotated for us."""
        tag_kept = out.getexif().get(_ORIENTATION_TAG) == _ROTATE_90
        pixels_rotated = out.height > out.width
        self.assertTrue(tag_kept or pixels_rotated, f"{label}: orientation lost - the image now renders 90 degrees wrong")

    def test_fixture_actually_carries_an_orientation(self) -> None:
        """Without this the assertions below could pass on a tagless fixture."""
        for fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
            back = PILImage.open(io.BytesIO(_image_with_orientation(fmt)))
            self.assertEqual(back.getexif().get(_ORIENTATION_TAG), _ROTATE_90, fmt)

    def test_png_keeps_its_orientation(self) -> None:
        """The format that lost it: PNG was absent from the EXIF-preserving set."""
        self._assert_displays_upright(self._downscaled("PNG", "png"), "PNG")

    def test_jpeg_keeps_its_orientation(self) -> None:
        self._assert_displays_upright(self._downscaled("JPEG", "jpg"), "JPEG")

    def test_tiff_still_displays_upright(self) -> None:
        """TIFF drops the tag but arrives pre-rotated, which is equally correct."""
        out = self._downscaled("TIFF", "tif")
        self.assertGreater(out.height, out.width, "TIFF should come back portrait, i.e. already rotated")

    def test_webp_conversion_keeps_its_orientation(self) -> None:
        self._assert_displays_upright(self._downscaled("JPEG", "jpg", convert_webp=True), "JPEG->WEBP")

    def test_stripping_gps_does_not_also_strip_orientation(self) -> None:
        """The strip rebuilds the EXIF block, so it could drop unrelated tags."""
        out = self._downscaled("JPEG", "jpg", strip_gps=True)

        self._assert_displays_upright(out, "JPEG with GPS stripped")
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD_TAG), "GPS should still be gone")
