"""The EXIF strip must scrub the stored file, for every format we accept and can rewrite.

Stripping is unconditional now - a stored file is served to everyone who can
reach the container it was contributed to, so the block comes off whatever the
uploader's settings say, and the values live on the ``Image`` row instead. What
"track pin visits" still governs is narrower: whether GPS is *recorded* on the
row at all (``Image.latitude``/``longitude`` and the ``GPSInfo`` key in the
``exif_data`` snapshot).

The per-format coverage below predates that change and survives it unaltered,
because two formats used to slip the strip for reasons that are still live:

- **TIFF** - the strip was gated on ``img.info["exif"]``, but TIFF carries EXIF
  in its own native IFD and leaves that key unset, so a GPS-tagged TIFF was
  never even examined. The current check reads ``getexif()`` as well for exactly
  this reason. TIFF is squarely in scope; the codebase notes elsewhere that
  "TIFFs, scanned PDFs and HEICs reach the gallery routinely".
- **AVIF** - accepted on upload, carries a GPS IFD, and returned early as
  "not a processable format" *before* reaching the strip.

Both are exercised end-to-end: author a file that genuinely carries GPS, run the
real function, and read the coordinates back off the bytes on disk. The
fixture-validity assertion matters as much as the strip assertion - an authoring
mistake that produced a GPS-less fixture would make every one of these pass while
testing nothing, which is exactly what happened on the way to finding this.
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

#: (Pillow format, stored extension) for every format the pipeline can rewrite.
_FORMATS = (("JPEG", "jpg"), ("TIFF", "tif"), ("WEBP", "webp"), ("PNG", "png"), ("AVIF", "avif"))


def _image_bytes_with_gps(fmt: str) -> bytes:
    """Encode a small image carrying a real GPS IFD.

    ``exif=`` must be handed raw bytes: passing the ``Exif`` object itself
    encodes without the GPS IFD for several of these formats, which yields a
    fixture that quietly proves nothing.
    """
    img = PILImage.new("RGB", (64, 48), (120, 30, 30))
    exif = img.getexif()
    gps = exif.get_ifd(_GPS_IFD_TAG)
    gps[1] = "N"
    gps[2] = (IFDRational(42), IFDRational(39), IFDRational(0))
    gps[3] = "W"
    gps[4] = (IFDRational(73), IFDRational(45), IFDRational(0))
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, exif=exif.tobytes())
    return buffer.getvalue()


def _has_gps(data: bytes) -> bool:
    return bool(PILImage.open(io.BytesIO(data)).getexif().get_ifd(_GPS_IFD_TAG))


class GpsStripByFormatTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_gps_strip_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _stored_bytes_after_strip(self, fmt: str, ext: str) -> bytes:
        image = baker.make(Image, image=None)
        image.image.save(f"gps.{ext}", ContentFile(_image_bytes_with_gps(fmt)), save=True)

        downscale_stored_image(image, max_dimension=None, convert_webp=False)

        # downscale_stored_image leaves persisting image.image.name to its caller,
        # so read the in-memory field rather than a refreshed row still pointing
        # at the replaced file.
        with image.image.open("rb") as handle:
            return handle.read()

    def test_fixtures_actually_carry_gps(self) -> None:
        """Without this, a bad fixture would make every strip assertion vacuous."""
        missing = [fmt for fmt, _ext in _FORMATS if not _has_gps(_image_bytes_with_gps(fmt))]

        self.assertEqual(missing, [], "fixture authoring produced no GPS IFD - the strip tests below would prove nothing")

    def test_gps_is_stripped_from_the_stored_file(self) -> None:
        leaked = [fmt for fmt, ext in _FORMATS if _has_gps(self._stored_bytes_after_strip(fmt, ext))]

        self.assertEqual(leaked, [], "GPS coordinates survived the strip in the stored file")

    def test_gps_goes_even_for_a_user_who_tracks_visits(self) -> None:
        """There is no opt-out from the file strip - only from recording it on the row.

        This asserted the opposite while the strip was opt-in. Keeping the
        coordinates in the served bytes is not something a visit-tracking
        preference should buy, and it was never what that preference described.
        """
        image = baker.make(Image, image=None)
        image.image.save("keep.jpg", ContentFile(_image_bytes_with_gps("JPEG")), save=True)

        downscale_stored_image(image, max_dimension=None, convert_webp=False)

        with image.image.open("rb") as handle:
            self.assertFalse(_has_gps(handle.read()))
