"""A HEIC upload gets its GPS removed like any other format.

Filed 2026-08-12: heic/heif were accepted uploads that Pillow could not open at
all, so a GPS strip a user had asked for raised, was logged as a warning, and
the file was stored with its full GPS IFD - the app promised a scrub and kept
the coordinates, for exactly the people who had opted out. HEIC is the iPhone
default, so this was not an edge case.

`pillow-heif` is now a dependency and its opener is registered in
`dashboard.apps.ready`, which makes HEIC an ordinary format everywhere: the
strip, thumbnailing and EXIF extraction all just work, and there is nothing for
a user to do differently.

These tests build a real HEIC carrying a real GPS IFD rather than asserting on
constants, because the failure being guarded against was precisely that the
file could not be opened - a test that never decodes one could not have caught
it.
"""

from __future__ import annotations

import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import downscale_stored_image

_GPS_IFD_TAG = 0x8825


def _heic_with_gps() -> bytes:
    """A small HEIC carrying a GPS IFD."""
    img = PILImage.new("RGB", (48, 48), (120, 80, 40))
    exif = PILImage.Exif()
    exif[_GPS_IFD_TAG] = {
        1: "N",
        2: (IFDRational(41), IFDRational(44), IFDRational(0)),
        3: "W",
        4: (IFDRational(73), IFDRational(55), IFDRational(0)),
    }
    buffer = io.BytesIO()
    img.save(buffer, format="HEIF", exif=exif.tobytes())
    return buffer.getvalue()


class HeicGpsStripTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _stored_heic(self) -> Image:
        image = baker.make(Image, profile=self.profile)
        image.image.save("IMG_0001.heic", SimpleUploadedFile("IMG_0001.heic", _heic_with_gps(), content_type="image/heic"), save=True)
        return image

    def _gps_present(self, image: Image) -> bool:
        with image.image.open("rb") as stored:
            return bool(PILImage.open(stored).getexif().get_ifd(_GPS_IFD_TAG))

    def test_pillow_can_open_a_heic_at_all(self) -> None:
        """The root cause: without pillow-heif this raises and everything downstream is skipped."""
        opened = PILImage.open(io.BytesIO(_heic_with_gps()))

        self.assertEqual(opened.format, "HEIF")

    def test_the_fixture_really_carries_gps(self) -> None:
        """Otherwise the strip test below would pass against a file that never had any."""
        self.assertTrue(self._gps_present(self._stored_heic()))

    def test_the_gps_is_removed(self) -> None:
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=False, strip_gps=True)
        image.save()

        self.assertFalse(self._gps_present(image), "the app promised to remove this and must actually do it")

    def test_the_photo_is_still_usable_afterwards(self) -> None:
        """A strip that corrupts the image would be a worse bug than the one it fixes."""
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=False, strip_gps=True)
        image.save()

        with image.image.open("rb") as stored:
            reopened = PILImage.open(stored)
            reopened.load()
        self.assertEqual(reopened.size, (48, 48))

    def test_gps_is_left_alone_when_no_strip_was_asked_for(self) -> None:
        """Stripping is the user's setting, not a default this format opts into."""
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=False, strip_gps=False)
        image.save()

        self.assertTrue(self._gps_present(image))

    def test_a_heic_upload_is_not_refused(self) -> None:
        """It just works now - there is nothing for the user to convert or re-enable."""
        from urbanlens.dashboard.models.images.model import MediaKind
        from urbanlens.dashboard.services.media.images import image_upload_error

        upload = SimpleUploadedFile("IMG_0001.heic", _heic_with_gps(), content_type="image/heic")

        self.assertIsNone(image_upload_error(upload, MediaKind.PHOTO))
