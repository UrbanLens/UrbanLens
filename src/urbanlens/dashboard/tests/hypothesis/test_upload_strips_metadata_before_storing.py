"""An accepted upload is stored already stripped, never raw-then-scrubbed.

The scrub used to belong entirely to ``tasks.process_image_upload``, which
means the file written to ``MEDIA_ROOT`` during the request was the uploader's
original - GPS block intact - and stayed that way until a Celery worker got to
it. It is served from there in the meantime.

``services.media.images.prepare_photo_upload`` closes that window by reading the
metadata and removing it from the bytes in the same step, before storage sees
them. Reading has to happen there too: ``exif_data``, ``taken_at``, the
attribution fields and the coordinates all come off the block being removed, and
the row is where the app's visibility rules can govern them.

What each half must hold:

- the stored file carries no coordinates, from the first instant it exists;
- the row carries them, so nothing downstream loses the position;
- the uploader's visit-tracking opt-out suppresses the *row* copy only - the
  file is scrubbed either way, which is not a setting.
"""

from __future__ import annotations

from fractions import Fraction
import io
from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.photos.photo_upload import upload_photo
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner

_GPS_IFD = 0x8825
#: EXIF Artist. Not 0x010F, which is Make - this test asserts the value comes
#: back as `Image.author`, and extract_author reads Artist.
_ARTIST_TAG = 0x013B


def _jpeg_with_gps(colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """A JPEG carrying a GPS position and an Artist tag."""
    exif = PILImage.Exif()
    exif[_ARTIST_TAG] = "A Photographer"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    gps[2] = (Fraction(42), Fraction(39), Fraction(9))
    gps[3] = "W"
    gps[4] = (Fraction(73), Fraction(45), Fraction(22))
    buf = io.BytesIO()
    PILImage.new("RGB", (48, 36), color=colour).save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="image/jpeg")


def _stored_exif(image: Image) -> dict:
    """Every EXIF tag the *stored* file still carries, GPS included."""
    with image.image.open("rb") as handle:
        opened = PILImage.open(handle)
        opened.load()
        exif = opened.getexif()
        tags = dict(exif)
        tags.update({f"gps{k}": v for k, v in exif.get_ifd(_GPS_IFD).items()})
    return tags


class UploadStripTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="ul_upload_strip_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.pin = Pin.objects.create(profile=self.profile, location=baker.make(Location, latitude="40.000000", longitude="-74.000000"))


class StoredFileIsStrippedTests(UploadStripTestCase):
    def test_the_fixture_carries_what_we_claim_to_remove(self):
        """Without this, every assertion below could pass vacuously."""
        opened = PILImage.open(io.BytesIO(_jpeg_with_gps()))

        self.assertTrue(dict(opened.getexif().get_ifd(_GPS_IFD)))
        self.assertEqual(opened.getexif().get(_ARTIST_TAG), "A Photographer")

    def test_upload_photo_stores_a_file_with_no_metadata(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertEqual(_stored_exif(image), {}, "the stored file still carries the metadata the uploader sent")

    def test_upload_photo_keeps_the_coordinates_on_the_row(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertIsNotNone(image.latitude)
        self.assertIsNotNone(image.longitude)
        self.assertAlmostEqual(float(image.latitude), 42.6525, places=3)
        self.assertAlmostEqual(float(image.longitude), -73.7561, places=3)

    def test_upload_photo_keeps_the_exif_snapshot_on_the_row(self):
        """The block is removed from the file, not lost."""
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertIsNotNone(image.exif_data)
        self.assertEqual(image.author, "A Photographer")

    def test_upload_photo_for_owner_stores_a_file_with_no_metadata(self):
        result = upload_photo_for_owner(self.pin, self.profile, _upload("owned.jpg", _jpeg_with_gps((90, 20, 30))))

        self.assertIsInstance(result, Image, f"fixture upload was rejected: {result}")
        self.assertEqual(_stored_exif(result), {})
        self.assertIsNotNone(result.latitude)

    def test_the_recorded_size_is_the_stored_size(self):
        """file_size drives the quota, so it has to describe what is on disk."""
        raw = _jpeg_with_gps()
        image = upload_photo(self.profile, _upload("shot.jpg", raw), pin=self.pin)

        self.assertEqual(image.file_size, image.image.size)
        self.assertLess(image.file_size, len(raw), "the strip should have made the file smaller")


class VisitTrackingOptOutTests(UploadStripTestCase):
    """The opt-out governs the row, never whether the file is scrubbed."""

    def setUp(self):
        super().setUp()
        Profile.objects.filter(pk=self.profile.pk).update(track_pin_visits=False)
        self.profile.refresh_from_db()

    def test_the_row_records_no_coordinates(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertIsNone(image.latitude)
        self.assertIsNone(image.longitude)

    def test_the_exif_snapshot_omits_the_gps_block(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertNotIn("GPSInfo", image.exif_data or {})

    def test_the_stored_file_is_still_scrubbed(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertEqual(_stored_exif(image), {})


class DedupStillMatchesTheUploadedBytesTests(UploadStripTestCase):
    """The checksum identifies what the user sent, not what we stored.

    Stripping changes the bytes, so a checksum taken after it would differ for
    every upload and the duplicate check would never fire again.
    """

    def test_the_same_file_uploaded_twice_is_refused(self):
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError

        raw = _jpeg_with_gps()
        upload_photo(self.profile, _upload("shot.jpg", raw), pin=self.pin)

        with pytest.raises(PhotoUploadError) as caught:
            upload_photo(self.profile, _upload("shot-again.jpg", raw), pin=self.pin)

        self.assertEqual(caught.value.status, 409)


class UnstrippableFormatsTests(UploadStripTestCase):
    """A format the byte-level stripper leaves alone is still accepted.

    It is stored as uploaded and scrubbed by the Celery re-encode, exactly as
    before - the sync strip narrows the window for the formats it handles, it
    does not gate acceptance.
    """

    def test_a_tiff_is_stored_unchanged(self):
        buf = io.BytesIO()
        PILImage.new("RGB", (48, 36), color=(1, 2, 3)).save(buf, format="TIFF")
        raw = buf.getvalue()

        image = upload_photo(self.profile, SimpleUploadedFile("scan.tif", raw, content_type="image/tiff"), pin=self.pin)

        self.assertEqual(Path(image.image.path).read_bytes(), raw)
