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
        image.image.save(
            "IMG_0001.heic", SimpleUploadedFile("IMG_0001.heic", _heic_with_gps(), content_type="image/heic"), save=True
        )
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

        downscale_stored_image(image, None, convert_webp=False)
        image.save()

        self.assertFalse(self._gps_present(image), "the app promised to remove this and must actually do it")

    def test_the_photo_is_still_usable_afterwards(self) -> None:
        """A strip that corrupts the image would be a worse bug than the one it fixes."""
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=False)
        image.save()

        with image.image.open("rb") as stored:
            reopened = PILImage.open(stored)
            reopened.load()
        self.assertEqual(reopened.size, (48, 48))

    def test_a_heic_upload_is_not_refused(self) -> None:
        """It just works now - there is nothing for the user to convert or re-enable."""
        from urbanlens.dashboard.models.images.model import MediaKind
        from urbanlens.dashboard.services.media.images import image_upload_error

        upload = SimpleUploadedFile("IMG_0001.heic", _heic_with_gps(), content_type="image/heic")

        self.assertIsNone(image_upload_error(upload, MediaKind.PHOTO))


class HeicIsStoredInARenderableFormatTests(TestCase):
    """Accepting the upload is only half of "HEIC just works".

    Commit 550b2cb8 removed the 415 that told the user to convert to JPEG
    first. But the WebP conversion is gated on the downscale policy, and HEIF is
    deliberately not a format the downscaler re-encodes - so a subscriber with
    downscaling off (the default) had their .heic stored verbatim and served
    through a plain `<img src>`, which every browser but Safari renders as a
    broken image. That is a worse outcome than the refusal it replaced: the
    refusal was actionable.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _stored_heic(self) -> Image:
        image = baker.make(Image, profile=self.profile)
        image.image.save(
            "IMG_0001.heic", SimpleUploadedFile("IMG_0001.heic", _heic_with_gps(), content_type="image/heic"), save=True
        )
        return image

    def _stored_format(self, image: Image) -> str:
        with image.image.open("rb") as stored:
            return PILImage.open(stored).format or ""

    def test_a_heic_is_transcoded_even_with_every_policy_switch_off(self) -> None:
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=False)
        image.save()

        self.assertEqual(self._stored_format(image), "JPEG")
        self.assertTrue(image.image.name.endswith(".jpg"), image.image.name)

    def test_webp_still_wins_when_the_policy_asks_for_it(self) -> None:
        """The transcode is a floor, not an override of the uploader's policy."""
        image = self._stored_heic()

        downscale_stored_image(image, None, convert_webp=True)
        image.save()

        self.assertEqual(self._stored_format(image), "WEBP")

    def test_the_transcode_pass_is_entered_at_all(self) -> None:
        """The gate in tasks.py skipped the whole pass when no policy switch was on."""
        from urbanlens.dashboard.services.media.images import stored_file_needs_transcode

        self.assertTrue(stored_file_needs_transcode("IMG_0001.heic"))
        self.assertTrue(stored_file_needs_transcode("IMG_0001.HEIF"))
        self.assertFalse(stored_file_needs_transcode("IMG_0001.jpg"))
        self.assertFalse(stored_file_needs_transcode(""))

    def test_a_jpeg_is_not_re_encoded_for_no_reason(self) -> None:
        """Only formats browsers cannot render are forced through the encoder."""
        image = baker.make(Image, profile=self.profile)
        buffer = io.BytesIO()
        PILImage.new("RGB", (48, 48), (10, 20, 30)).save(buffer, format="JPEG")
        image.image.save(
            "plain.jpg", SimpleUploadedFile("plain.jpg", buffer.getvalue(), content_type="image/jpeg"), save=True
        )

        self.assertIsNone(downscale_stored_image(image, None, convert_webp=False))
