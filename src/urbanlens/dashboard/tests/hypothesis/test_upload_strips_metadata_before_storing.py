"""An accepted upload is stored raw, then stripped by the task that reads it.

Storing the raw upload and reading/stripping it in ``tasks.process_image_upload``
(rather than in the request, as this used to work) is what keeps the request
from ever running a Pillow decode over attacker-supplied bytes - see
``services.sandbox.guard`` for why that decode has to happen somewhere else
entirely. The window this reopens - the stored file is the uploader's raw
bytes, GPS block intact, until the task gets to it - is closed by
``Image.pending_scan`` instead of by scrubbing the bytes before anyone can look:
a pending row is invisible to everyone but its uploader
(``services.media.access.authorize_image``, ``ImageQuerySet.visible_to``), so
nobody is ever served the raw file. ``test_photo_pending_scan.py`` covers that
half; this file covers what the task itself produces once it runs.

What each half must hold:

- immediately after upload: the stored file is exactly the uploaded bytes
  (metadata and all), the row carries none of it yet, and ``pending_scan`` is
  True;
- after ``process_image_upload`` runs: the stored file carries no metadata, the
  row carries what was in it, and ``pending_scan`` is False;
- the uploader's visit-tracking opt-out suppresses the *row* copy of
  coordinates only - the file is scrubbed either way, which is not a setting.
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


def _process(image: Image) -> Image:
    """Run the sandboxed task that reads and strips *image*, then refresh it.

    Neither ``upload_photo`` nor ``upload_photo_for_owner`` runs this
    synchronously any more - see the module docstring - so every test below
    that wants the *processed* result calls this after uploading.
    """
    from urbanlens.dashboard.tasks import process_image_upload

    process_image_upload(image.pk)
    image.refresh_from_db()
    return image


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

    def test_upload_photo_stores_the_upload_raw_then_strips_it_once_processed(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertTrue(image.pending_scan)
        self.assertNotEqual(_stored_exif(image), {}, "the raw upload should still be exactly what was sent")

        image = _process(image)

        self.assertFalse(image.pending_scan)
        self.assertEqual(_stored_exif(image), {}, "the stored file still carries the metadata the uploader sent")

    def test_upload_photo_keeps_the_coordinates_on_the_row(self):
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertIsNone(image.latitude, "coordinates are read by the task, not the request")
        self.assertIsNone(image.longitude)

        image = _process(image)

        self.assertIsNotNone(image.latitude)
        self.assertIsNotNone(image.longitude)
        self.assertAlmostEqual(float(image.latitude), 42.6525, places=3)
        self.assertAlmostEqual(float(image.longitude), -73.7561, places=3)

    def test_upload_photo_keeps_the_exif_snapshot_on_the_row(self):
        """The block is removed from the file, not lost."""
        image = upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin)

        self.assertIsNone(image.exif_data)
        self.assertIsNone(image.author)

        image = _process(image)

        self.assertIsNotNone(image.exif_data)
        self.assertEqual(image.author, "A Photographer")

    def test_upload_photo_for_owner_stores_a_file_that_ends_with_no_metadata(self):
        result = upload_photo_for_owner(self.pin, self.profile, _upload("owned.jpg", _jpeg_with_gps((90, 20, 30))))

        self.assertIsInstance(result, Image, f"fixture upload was rejected: {result}")
        self.assertTrue(result.pending_scan)
        self.assertIsNone(result.latitude)

        result = _process(result)

        self.assertEqual(_stored_exif(result), {})
        self.assertIsNotNone(result.latitude)

    def test_the_recorded_size_tracks_the_stored_size_at_every_stage(self):
        """file_size drives the quota, so it has to describe what is on disk."""
        raw = _jpeg_with_gps()
        image = upload_photo(self.profile, _upload("shot.jpg", raw), pin=self.pin)

        self.assertEqual(image.file_size, len(raw), "before processing, the recorded size is the raw upload's own")

        image = _process(image)

        self.assertEqual(image.file_size, image.image.size)
        self.assertLess(image.file_size, len(raw), "the strip should have made the file smaller")


class VisitTrackingOptOutTests(UploadStripTestCase):
    """The opt-out governs the row, never whether the file is scrubbed."""

    def setUp(self):
        super().setUp()
        Profile.objects.filter(pk=self.profile.pk).update(track_pin_visits=False)
        self.profile.refresh_from_db()

    def test_the_row_records_no_coordinates(self):
        # Not yet meaningful before processing - nothing has been read off the
        # file at all at this point, opt-out or not - so this asserts the
        # opt-out is honoured, not merely that reading hasn't happened yet.
        image = _process(upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin))

        self.assertIsNone(image.latitude)
        self.assertIsNone(image.longitude)

    def test_the_exif_snapshot_omits_the_gps_block(self):
        image = _process(upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin))

        self.assertNotIn("GPSInfo", image.exif_data or {})

    def test_the_stored_file_is_still_scrubbed(self):
        image = _process(upload_photo(self.profile, _upload("shot.jpg", _jpeg_with_gps()), pin=self.pin))

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
    """A format ``downscale_stored_image`` cannot rewrite is still accepted.

    Stored exactly as uploaded until (or unless) the task's re-encode produces a
    format it can rewrite - acceptance never depends on that.
    """

    def test_a_tiff_is_stored_unchanged(self):
        buf = io.BytesIO()
        PILImage.new("RGB", (48, 36), color=(1, 2, 3)).save(buf, format="TIFF")
        raw = buf.getvalue()

        image = upload_photo(self.profile, SimpleUploadedFile("scan.tif", raw, content_type="image/tiff"), pin=self.pin)

        self.assertEqual(Path(image.image.path).read_bytes(), raw)
