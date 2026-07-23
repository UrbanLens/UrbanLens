"""Tests for Image attribution field extraction and auto-population.

Covers:
- extract_author/extract_copyright_notice/extract_caption_from_metadata - EXIF
  Artist/Copyright/ImageDescription tags
- is_camera_generated_filename() - phone/camera auto-naming pattern matching
- process_image_upload() - the uploader-as-author fallback for unattributed
  camera-named photos, and that it does not apply to other filenames
"""

from __future__ import annotations

import io
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from hypothesis import given, strategies as st
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.images import extract_author, extract_caption_from_metadata, extract_copyright_notice, is_camera_generated_filename
from urbanlens.dashboard.tasks import process_image_upload

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")

_CAMERA_PREFIXES = ("pxl", "img", "mvimg", "dsc", "dscn", "dcim")


def _jpeg_bytes(*, artist: str | None = None, copyright_notice: str | None = None, description: str | None = None) -> bytes:
    """Build an in-memory JPEG, optionally carrying EXIF Artist/Copyright/ImageDescription tags."""
    img = PILImage.new("RGB", (60, 40), color=(10, 20, 30))
    buf = io.BytesIO()
    exif = PILImage.Exif()
    if artist is not None:
        exif[0x013B] = artist
    if copyright_notice is not None:
        exif[0x8298] = copyright_notice
    if description is not None:
        exif[0x010E] = description
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


class ExtractAttributionTests(TestCase):
    """extract_author/extract_copyright_notice/extract_caption_from_metadata read EXIF tags."""

    def test_extract_author_reads_artist_tag(self):
        self.assertEqual(extract_author(io.BytesIO(_jpeg_bytes(artist="Jane Doe"))), "Jane Doe")

    def test_extract_author_none_when_absent(self):
        self.assertIsNone(extract_author(io.BytesIO(_jpeg_bytes())))

    def test_extract_copyright_reads_tag(self):
        self.assertEqual(extract_copyright_notice(io.BytesIO(_jpeg_bytes(copyright_notice="(c) 2026 Jane Doe"))), "(c) 2026 Jane Doe")

    def test_extract_copyright_none_when_absent(self):
        self.assertIsNone(extract_copyright_notice(io.BytesIO(_jpeg_bytes())))

    def test_extract_caption_reads_description_tag(self):
        self.assertEqual(extract_caption_from_metadata(io.BytesIO(_jpeg_bytes(description="Sunset over the bay"))), "Sunset over the bay")

    def test_extract_caption_none_when_absent(self):
        self.assertIsNone(extract_caption_from_metadata(io.BytesIO(_jpeg_bytes())))


class CameraFilenameTests(SimpleTestCase):
    """is_camera_generated_filename() recognizes common phone/camera naming conventions."""

    @given(st.sampled_from(_CAMERA_PREFIXES + tuple(p.upper() for p in _CAMERA_PREFIXES)), st.integers(min_value=1000, max_value=99999999))
    def test_matches_known_camera_prefixes(self, prefix, number):
        self.assertTrue(is_camera_generated_filename(f"{prefix}_{number}.jpg"))
        self.assertTrue(is_camera_generated_filename(f"{prefix}-{number}.jpg"))

    def test_matches_pixel_example(self):
        self.assertTrue(is_camera_generated_filename("PXL_20260709_123456.jpg"))

    def test_matches_whatsapp_example(self):
        self.assertTrue(is_camera_generated_filename("IMG-20260709-WA0001.jpg"))

    def test_matches_regardless_of_directory_prefix(self):
        self.assertTrue(is_camera_generated_filename("pin_images/PXL_20260709_123456.jpg"))

    @given(st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=3, max_size=20))
    def test_descriptive_names_do_not_match(self, name):
        if name.startswith(_CAMERA_PREFIXES):
            return  # hypothesis occasionally generates a real camera prefix
        self.assertFalse(is_camera_generated_filename(f"{name}.jpg"))

    def test_plain_descriptive_name_does_not_match(self):
        self.assertFalse(is_camera_generated_filename("abandoned-hospital-stairwell.jpg"))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ProcessImageUploadAttributionTests(TestCase):
    """process_image_upload() infers the uploader as author only for unattributed camera-named photos."""

    def _make_image_row(self, content: bytes, name: str) -> Image:
        profile = User.objects.create(username=f"u{Image.objects.count()}", first_name="Jane", last_name="Doe").profile
        return Image.objects.create(image=SimpleUploadedFile(name, content, content_type="image/jpeg"), profile=profile)

    def test_camera_named_photo_without_metadata_gets_uploader_as_author(self):
        row = self._make_image_row(_jpeg_bytes(), "PXL_20260709_123456.jpg")
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.author, "Jane Doe")

    def test_descriptively_named_photo_without_metadata_is_left_unattributed(self):
        row = self._make_image_row(_jpeg_bytes(), "abandoned-hospital-stairwell.jpg")
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()
        self.assertIsNone(row.author)

    def test_camera_named_photo_with_existing_exif_author_is_not_overridden(self):
        row = self._make_image_row(_jpeg_bytes(artist="Original Photographer"), "PXL_20260709_123456.jpg")
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.author, "Original Photographer")
