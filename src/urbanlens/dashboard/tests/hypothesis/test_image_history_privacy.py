"""Tests for photo GPS/location privacy when a profile has visit-history tracking off.

Covers process_image_upload()'s ``strip_location`` behavior:
- Image.latitude/longitude are never populated from EXIF GPS
- the stored file's own embedded GPS EXIF tag is stripped, even when no
  resize/WebP conversion would otherwise be needed
- exif_data never carries a GPSInfo block
- no VisitSuggestion is raised from the photo
"""

from __future__ import annotations

import io
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.tasks import process_image_upload

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _geotagged_jpeg_bytes() -> bytes:
    """Build an in-memory JPEG carrying a GPS IFD (40.0, -74.0) and a Make tag."""
    img = PILImage.new("RGB", (60, 40), color=(10, 20, 30))
    exif = PILImage.Exif()
    exif[0x010F] = "UrbanLens"  # Make
    gps_ifd = exif.get_ifd(0x8825)  # 34853 - GPSInfo IFD
    gps_ifd[1] = "N"  # GPSLatitudeRef
    gps_ifd[2] = (IFDRational(40, 1), IFDRational(0, 1), IFDRational(0, 1))  # GPSLatitude
    gps_ifd[3] = "W"  # GPSLongitudeRef
    gps_ifd[4] = (IFDRational(74, 1), IFDRational(0, 1), IFDRational(0, 1))  # GPSLongitude
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ProcessImageUploadLocationPrivacyTests(TestCase):
    """process_image_upload() strips GPS everywhere when track_pin_visits is off."""

    def _make_image_row(self, *, track_pin_visits: bool) -> Image:
        profile = User.objects.create(username=f"u{Image.objects.count()}").profile
        profile.track_pin_visits = track_pin_visits
        profile.save(update_fields=["track_pin_visits"])
        return Image.objects.create(image=SimpleUploadedFile("photo.jpg", _geotagged_jpeg_bytes(), content_type="image/jpeg"), profile=profile)

    def test_tracking_off_strips_lat_lng_exif_and_file(self):
        row = self._make_image_row(track_pin_visits=False)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()

        self.assertIsNone(row.latitude)
        self.assertIsNone(row.longitude)
        self.assertIsNotNone(row.exif_data)
        self.assertNotIn("GPSInfo", row.exif_data)

        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            exif = stored.getexif()
            self.assertFalse(exif.get_ifd(0x8825))
            self.assertEqual(exif[0x010F], "UrbanLens")  # non-GPS EXIF survives

    def test_tracking_off_raises_no_visit_suggestion(self):
        row = self._make_image_row(track_pin_visits=False)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        self.assertEqual(VisitSuggestion.objects.count(), 0)

    def test_tracking_on_preserves_lat_lng_and_exif_gps(self):
        row = self._make_image_row(track_pin_visits=True)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()

        self.assertAlmostEqual(float(row.latitude), 40.0, places=4)
        self.assertAlmostEqual(float(row.longitude), -74.0, places=4)
        self.assertIn("GPSInfo", row.exif_data)

        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertTrue(stored.getexif().get_ifd(0x8825))
