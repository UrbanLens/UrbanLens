"""Tests for EXIF preservation and the upload downscale/WebP pipeline.

Covers:
- _json_safe() - EXIF values (bytes, rationals, NaN, nesting) become JSON-safe
- extract_exif_data() - snapshots EXIF tags by name before any conversion
- downscale_stored_image() - resizes over-large files, converts to WebP,
  preserves EXIF in the re-encoded file, and leaves small/exotic files alone
"""

from __future__ import annotations

import io
import json
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.images import _json_safe, downscale_stored_image, extract_exif_data

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _jpeg_bytes(width: int, height: int, with_exif: bool = True) -> bytes:
    """Build an in-memory JPEG, optionally carrying EXIF Make/Model tags."""
    img = PILImage.new("RGB", (width, height), color=(120, 60, 30))
    buf = io.BytesIO()
    if with_exif:
        exif = PILImage.Exif()
        exif[0x010F] = "UrbanLens"  # Make
        exif[0x0110] = "TestCam 3000"  # Model
        img.save(buf, format="JPEG", exif=exif.tobytes())
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_image_row(content: bytes, name: str = "photo.jpg") -> Image:
    profile = User.objects.create(username=f"u{Image.objects.count()}").profile
    return Image.objects.create(image=SimpleUploadedFile(name, content, content_type="image/jpeg"), profile=profile)


class JsonSafeTests(TestCase):
    """_json_safe() reduces EXIF values to JSON-serializable types."""

    def test_scalars_pass_through(self):
        for value in (None, True, 3, "text", 2.5):
            self.assertEqual(_json_safe(value), value)

    def test_small_bytes_become_hex(self):
        self.assertEqual(_json_safe(b"\x01\x02"), "0102")

    def test_huge_bytes_are_summarized(self):
        blob = b"\x00" * 10_000
        self.assertEqual(_json_safe(blob), "<10000 bytes>")

    def test_rational_becomes_float(self):
        self.assertEqual(_json_safe(IFDRational(1, 2)), 0.5)

    def test_zero_denominator_rational_is_stringified(self):
        result = _json_safe(IFDRational(1, 0))
        self.assertIsInstance(result, (str, float))
        json.dumps(result)

    def test_nan_is_stringified(self):
        self.assertIsInstance(_json_safe(float("nan")), str)

    def test_nested_structures(self):
        result = _json_safe((IFDRational(1, 4), b"\xff", {"k": IFDRational(3, 2)}))
        json.dumps(result)
        self.assertEqual(result, [0.25, "ff", {"k": 1.5}])


class ExtractExifDataTests(TestCase):
    """extract_exif_data() snapshots tags by human-readable name."""

    def test_reads_tags_by_name(self):
        data = extract_exif_data(io.BytesIO(_jpeg_bytes(50, 40)))
        self.assertIsNotNone(data)
        self.assertEqual(data["Make"], "UrbanLens")
        self.assertEqual(data["Model"], "TestCam 3000")
        json.dumps(data)

    def test_none_without_exif(self):
        self.assertIsNone(extract_exif_data(io.BytesIO(_jpeg_bytes(50, 40, with_exif=False))))

    def test_none_for_garbage(self):
        self.assertIsNone(extract_exif_data(io.BytesIO(b"not an image")))

    def test_rewinds_file(self):
        fh = io.BytesIO(_jpeg_bytes(50, 40))
        extract_exif_data(fh)
        self.assertEqual(fh.tell(), 0)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class DownscaleStoredImageTests(TestCase):
    """downscale_stored_image() resizes/converts stored files in place."""

    def test_oversized_jpeg_is_resized_and_keeps_exif(self):
        row = _make_image_row(_jpeg_bytes(1600, 1200))
        old_size = row.image.size
        new_size = downscale_stored_image(row, max_dimension=800, convert_webp=False)

        self.assertIsNotNone(new_size)
        self.assertLess(new_size, old_size)
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertEqual(stored.format, "JPEG")
            self.assertLessEqual(max(stored.size), 800)
            self.assertEqual(stored.getexif()[0x0110], "TestCam 3000")

    def test_small_file_left_untouched(self):
        row = _make_image_row(_jpeg_bytes(400, 300))
        old_name = row.image.name
        self.assertIsNone(downscale_stored_image(row, max_dimension=800, convert_webp=False))
        self.assertEqual(row.image.name, old_name)

    def test_webp_conversion_replaces_file_and_keeps_exif(self):
        row = _make_image_row(_jpeg_bytes(400, 300))
        old_name = row.image.name
        new_size = downscale_stored_image(row, max_dimension=None, convert_webp=True)

        self.assertIsNotNone(new_size)
        self.assertTrue(row.image.name.endswith(".webp"))
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertEqual(stored.format, "WEBP")
            self.assertEqual(stored.getexif()[0x010F], "UrbanLens")
        # The original file is removed from storage.
        self.assertFalse(row.image.storage.exists(old_name))

    def test_resize_and_convert_together(self):
        row = _make_image_row(_jpeg_bytes(1600, 1200))
        new_size = downscale_stored_image(row, max_dimension=640, convert_webp=True)
        self.assertIsNotNone(new_size)
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertEqual(stored.format, "WEBP")
            self.assertLessEqual(max(stored.size), 640)

    def test_unprocessable_format_is_skipped(self):
        buf = io.BytesIO()
        PILImage.new("P", (900, 900)).save(buf, format="GIF")
        row = _make_image_row(buf.getvalue(), name="anim.gif")
        self.assertIsNone(downscale_stored_image(row, max_dimension=200, convert_webp=True))
