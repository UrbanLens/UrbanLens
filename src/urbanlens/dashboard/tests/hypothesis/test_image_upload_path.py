"""Tests for Image.image's upload_to path handling.

Covers the SuspiciousFileOperation regression: an uploaded filename longer
than the field's old default max_length (100) - real-world archival/scan
filenames routinely are - overflowed Storage.get_available_name's truncation
math and crashed the upload outright instead of storing the file. Now doubly
moot for length purposes (the stored stem is a fixed-length opaque token,
never the uploaded name), but still worth proving nothing regresses for an
extreme filename.

Every generated path now carries a random ``<bucket>/<token>/`` directory
ahead of an opaque filename (see ``pin_image_upload_path``'s docstring - the
directory is what stops a filename from being a guessable URL; the opaque
name is what stops the URL from spelling out the uploaded file's own name).
"""

from __future__ import annotations

import re
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from hypothesis import given, strategies as st
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image, pin_image_upload_path

#: `pin_images/<2-char bucket>/<random token>/<filename>`.
_RANDOM_DIR_RE = re.compile(r"^pin_images/[A-Za-z0-9_-]{2}/[A-Za-z0-9_-]+/(.+)$")


def _filename_segment(path: str) -> str:
    """The filename `pin_image_upload_path` produced, stripped of its random directory."""
    match = _RANDOM_DIR_RE.match(path)
    assert match, f"{path!r} does not look like pin_images/<bucket>/<token>/<filename>"
    return match.group(1)


_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")

# The filename that triggered the production crash: a real archival-photo
# title, well past the field's old 100-character default max_length once
# "pin_images/" and Django's own dedupe suffix are accounted for.
_OVERLONG_FILENAME = (
    "POS-0005_-_October_1st_1941_Postmarked_-_Birdseye_View_of_State_Hospital_Poughkeepsie_N._Y._-_Historic.jpg"
)


def _jpeg_bytes() -> bytes:
    img = PILImage.new("RGB", (60, 40), color=(10, 20, 30))
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class PinImageUploadPathTests(SimpleTestCase):
    """pin_image_upload_path() never leaks the uploaded filename and stays well under max_length."""

    def test_short_name_produces_an_opaque_filename(self):
        path = pin_image_upload_path(Image(), "photo.jpg")
        self.assertTrue(_RANDOM_DIR_RE.match(path), path)
        self.assertNotIn("photo", _filename_segment(path))
        self.assertTrue(path.endswith(".jpg"))

    def test_two_uploads_of_the_same_name_get_different_directories_and_filenames(self):
        first = pin_image_upload_path(Image(), "photo.jpg")
        second = pin_image_upload_path(Image(), "photo.jpg")
        self.assertNotEqual(
            first, second, "both the directory and the filename must differ per upload, or the path is guessable"
        )

    def test_overlong_name_never_reaches_storage(self):
        path = pin_image_upload_path(Image(), _OVERLONG_FILENAME)
        self.assertLessEqual(len(path), Image._meta.get_field("image").max_length)
        self.assertNotIn("POS-0005", path)
        self.assertTrue(path.endswith(".jpg"))

    def test_camera_prefix_never_reaches_storage(self):
        # is_camera_generated_filename() used to match on the *stored* name's
        # prefix; it now reads Image.original_filename instead (see
        # test_image_attribution.py), specifically so nothing recognisable
        # from the upload has to survive into the stored path at all.
        long_camera_name = "PXL_20260709_123456" + ("_extra" * 20) + ".jpg"
        path = pin_image_upload_path(Image(), long_camera_name)
        self.assertNotIn("PXL_20260709_123456", path)

    def test_capture_year_is_embedded_when_known(self):
        from datetime import datetime

        from django.utils import timezone

        instance = Image(taken_at=timezone.make_aware(datetime(2026, 7, 9)))
        path = pin_image_upload_path(instance, "photo.jpg")
        self.assertTrue(_filename_segment(path).startswith("2026-"), path)

    @given(st.text(min_size=1, max_size=300), st.sampled_from([".jpg", ".png", ".jpeg", ".heic", ""]))
    def test_generated_path_always_fits_field_max_length(self, stem, ext):
        path = pin_image_upload_path(Image(), f"{stem}{ext}")
        self.assertLessEqual(len(path), Image._meta.get_field("image").max_length)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ImageUploadOverlongFilenameTests(TestCase):
    """Creating an Image row with a real-world overlong filename must not raise SuspiciousFileOperation."""

    def test_overlong_filename_upload_succeeds(self):
        image = Image.objects.create(
            image=SimpleUploadedFile(_OVERLONG_FILENAME, _jpeg_bytes(), content_type="image/jpeg")
        )
        self.assertNotIn("POS-0005", image.image.name)
        self.assertEqual(image.original_filename, _OVERLONG_FILENAME)
