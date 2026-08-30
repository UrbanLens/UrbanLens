"""Tests for Image.image's upload_to path handling.

Covers the SuspiciousFileOperation regression: an uploaded filename longer
than the field's old default max_length (100) - real-world archival/scan
filenames routinely are - overflowed Storage.get_available_name's truncation
math and crashed the upload outright instead of storing the file.

Every generated path now carries a random ``<bucket>/<token>/`` directory
ahead of the filename (see ``pin_image_upload_path``'s docstring - it is what
stops a filename from being a guessable URL), so assertions here match on the
filename's own segment rather than the exact path.
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
_OVERLONG_FILENAME = "POS-0005_-_October_1st_1941_Postmarked_-_Birdseye_View_of_State_Hospital_Poughkeepsie_N._Y._-_Historic.jpg"


def _jpeg_bytes() -> bytes:
    img = PILImage.new("RGB", (60, 40), color=(10, 20, 30))
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class PinImageUploadPathTests(SimpleTestCase):
    """pin_image_upload_path() keeps every generated path well under the field's max_length."""

    def test_short_name_passes_through(self):
        path = pin_image_upload_path(None, "photo.jpg")
        self.assertTrue(_RANDOM_DIR_RE.match(path), path)
        self.assertEqual(_filename_segment(path), "photo.jpg")

    def test_two_uploads_of_the_same_name_get_different_directories(self):
        first = pin_image_upload_path(None, "photo.jpg")
        second = pin_image_upload_path(None, "photo.jpg")
        self.assertNotEqual(first, second, "the random directory must differ per upload, or the path is guessable")

    def test_overlong_name_is_trimmed(self):
        path = pin_image_upload_path(None, _OVERLONG_FILENAME)
        # Not the field's old 100-char default this fixture was chosen to
        # overflow - the random directory now sits ahead of the trimmed stem
        # too, so the bound to check is the field's actual max_length.
        self.assertLessEqual(len(path), Image._meta.get_field("image").max_length)
        self.assertTrue(_filename_segment(path).startswith("POS-0005"))
        self.assertTrue(path.endswith(".jpg"))

    def test_camera_prefix_survives_trimming(self):
        # is_camera_generated_filename() matches on the *stored* name's
        # prefix - a long camera-named file must still start with it.
        long_camera_name = "PXL_20260709_123456" + ("_extra" * 20) + ".jpg"
        path = pin_image_upload_path(None, long_camera_name)
        self.assertTrue(_filename_segment(path).startswith("PXL_20260709_123456"))

    @given(st.text(min_size=1, max_size=300), st.sampled_from([".jpg", ".png", ".jpeg", ".heic", ""]))
    def test_generated_path_always_fits_field_max_length(self, stem, ext):
        path = pin_image_upload_path(None, f"{stem}{ext}")
        self.assertLessEqual(len(path), Image._meta.get_field("image").max_length)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ImageUploadOverlongFilenameTests(TestCase):
    """Creating an Image row with a real-world overlong filename must not raise SuspiciousFileOperation."""

    def test_overlong_filename_upload_succeeds(self):
        image = Image.objects.create(image=SimpleUploadedFile(_OVERLONG_FILENAME, _jpeg_bytes(), content_type="image/jpeg"))
        self.assertTrue(_filename_segment(image.image.name).startswith("POS-0005"), image.image.name)
