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

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import _json_safe, downscale_stored_image, extract_exif_data

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _jpeg_bytes(width: int, height: int, with_exif: bool = True, with_gps: bool = False) -> bytes:
    """Build an in-memory JPEG, optionally carrying EXIF Make/Model tags and/or a GPS IFD."""
    img = PILImage.new("RGB", (width, height), color=(120, 60, 30))
    buf = io.BytesIO()
    if with_exif:
        exif = PILImage.Exif()
        exif[0x010F] = "UrbanLens"  # Make
        exif[0x0110] = "TestCam 3000"  # Model
        if with_gps:
            gps_ifd = exif.get_ifd(0x8825)  # 34853 - GPSInfo IFD
            gps_ifd[1] = "N"  # GPSLatitudeRef
            gps_ifd[2] = (IFDRational(40, 1), IFDRational(0, 1), IFDRational(0, 1))  # GPSLatitude
            gps_ifd[3] = "W"  # GPSLongitudeRef
            gps_ifd[4] = (IFDRational(74, 1), IFDRational(0, 1), IFDRational(0, 1))  # GPSLongitude
        img.save(buf, format="JPEG", exif=exif.tobytes())
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def _mpo_bytes(width: int = 64, height: int = 48) -> bytes:
    """Build a real multi-picture JPEG carrying a GPS IFD on its first frame.

    Phones produce these for depth and second-lens captures, usually named
    ``.jpg``. Pillow reports the format as ``MPO`` and loads only frame 0.
    """
    first = PILImage.new("RGB", (width, height), color=(200, 10, 10))
    second = PILImage.new("RGB", (width, height), color=(10, 10, 200))
    exif = PILImage.Exif()
    exif[0x010F] = "UrbanLens"
    gps_ifd = exif.get_ifd(0x8825)
    gps_ifd[1] = "N"
    gps_ifd[2] = (IFDRational(40, 1), IFDRational(0, 1), IFDRational(0, 1))
    buf = io.BytesIO()
    first.save(buf, format="MPO", append_images=[second], exif=exif)
    return buf.getvalue()


def _make_image_row(content: bytes, name: str = "photo.jpg") -> Image:
    profile = User.objects.create(username=f"u{Image.objects.count()}").profile
    return Image.objects.create(image=SimpleUploadedFile(name, content, content_type="image/jpeg"), profile=profile)


class JsonSafeTests(SimpleTestCase):
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

    def test_oversized_jpeg_is_resized_and_drops_exif(self):
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
            self.assertIsNone(stored.getexif().get(0x0110), "the camera model rode along into the stored file")

    def test_a_file_another_row_shares_is_not_deleted_when_replaced(self):
        """Pin sharing points two rows at one storage key; re-encoding one must not blank the other.

        ``services.sharing.pin_sharing`` copies a shared pin's photos by reusing
        the same ``image`` name rather than duplicating bytes, and the
        ``strip_exif_from_stored_photos`` command re-encodes every stored photo
        in turn. Deleting the old name unconditionally destroyed the other
        profile's copy, with only a broken image to show for it.
        """
        row = _make_image_row(_jpeg_bytes(1600, 1200))
        shared_name = row.image.name
        storage = row.image.storage

        sibling = _make_image_row(_jpeg_bytes(64, 64, with_exif=False))
        sibling.image.name = shared_name
        sibling.save(update_fields=["image"])

        self.assertIsNotNone(downscale_stored_image(row, max_dimension=800, convert_webp=False))
        self.assertNotEqual(row.image.name, shared_name)
        self.assertTrue(storage.exists(shared_name), "the row that still points at this file lost it")

        # Once nothing else references the old name, replacing it does clean up -
        # the guard must not turn every re-encode into a leaked file.
        sibling.delete()
        row.save(update_fields=["image"])
        stale = row.image.name
        self.assertIsNotNone(downscale_stored_image(row, max_dimension=400, convert_webp=False))
        self.assertNotEqual(row.image.name, stale)
        self.assertFalse(storage.exists(stale), "an unshared replaced file should not be left behind")

    def test_small_file_without_exif_is_left_untouched(self):
        """Nothing to shrink, nothing to convert, nothing to strip."""
        row = _make_image_row(_jpeg_bytes(400, 300, with_exif=False))
        old_name = row.image.name
        self.assertIsNone(downscale_stored_image(row, max_dimension=800, convert_webp=False))
        self.assertEqual(row.image.name, old_name)

    def test_small_file_with_exif_is_rewritten_to_strip_it(self):
        """Carrying EXIF is itself a reason to re-save, size notwithstanding."""
        row = _make_image_row(_jpeg_bytes(400, 300))

        self.assertIsNotNone(downscale_stored_image(row, max_dimension=800, convert_webp=False))
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertIsNone(stored.getexif().get(0x0110))

    def test_webp_conversion_replaces_file_and_drops_exif(self):
        row = _make_image_row(_jpeg_bytes(400, 300))
        old_name = row.image.name
        new_size = downscale_stored_image(row, max_dimension=None, convert_webp=True)

        self.assertIsNotNone(new_size)
        self.assertTrue(row.image.name.endswith(".webp"))
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertEqual(stored.format, "WEBP")
            self.assertIsNone(stored.getexif().get(0x010F), "the camera make survived the WebP conversion")
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


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class GpsIsStrippedWithoutBeingAskedTests(TestCase):
    """GPS removal is unconditional, not a setting the uploader has to find.

    This class used to exercise a ``strip_gps`` flag. The flag is gone: a stored
    file is served to everyone who can reach the container it was contributed to,
    so the whole EXIF block comes off every time, and there is no opt-out to get
    wrong. What survives is on the ``Image`` row, behind the app's visibility rules.
    """

    def test_gps_is_removed_even_when_no_resize_is_needed(self):
        row = _make_image_row(_jpeg_bytes(400, 300, with_gps=True))

        new_size = downscale_stored_image(row, max_dimension=800, convert_webp=False)

        self.assertIsNotNone(new_size, "a small GPS-tagged file was left exactly as uploaded")
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertFalse(stored.getexif().get_ifd(0x8825), "GPS coordinates were served inside the photo")

    def test_gps_is_removed_alongside_a_resize(self):
        row = _make_image_row(_jpeg_bytes(1600, 1200, with_gps=True))

        new_size = downscale_stored_image(row, max_dimension=800, convert_webp=False)

        self.assertIsNotNone(new_size)
        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            self.assertLessEqual(max(stored.size), 800)
            self.assertFalse(stored.getexif().get_ifd(0x8825))

    def test_the_rest_of_the_block_goes_too(self):
        """Not just GPS: make and model identify the photographer's kit."""
        row = _make_image_row(_jpeg_bytes(400, 300, with_gps=True))

        downscale_stored_image(row, max_dimension=800, convert_webp=False)

        with row.image.open("rb") as fh:
            stored = PILImage.open(fh)
            stored.load()
            exif = stored.getexif()
            self.assertIsNone(exif.get(0x010F))
            self.assertIsNone(exif.get(0x0110))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class MultiPictureJpegTests(TestCase):
    """A multi-picture JPEG must not skip the strip by being an unlisted format.

    MPO is a JPEG container holding several images; Pillow reports it as its
    own format, which was in none of this module's format sets, so
    downscale_stored_image returned before doing anything and the file was kept
    byte-for-byte - GPS block intact - however the uploader's settings were set.
    """

    def test_the_source_fixture_really_is_a_multi_picture_jpeg(self):
        """Guards the test itself: two concatenated JPEGs would read as JPEG."""
        opened = PILImage.open(io.BytesIO(_mpo_bytes()))

        self.assertEqual(opened.format, "MPO")
        self.assertEqual(getattr(opened, "n_frames", 1), 2)
        self.assertTrue(dict(opened.getexif().get_ifd(0x8825)), "fixture should carry GPS to strip")

    def test_gps_is_stripped_from_a_multi_picture_jpeg(self):
        image = _make_image_row(_mpo_bytes(), name="depth.jpg")

        written = downscale_stored_image(image, max_dimension=None, convert_webp=False)

        self.assertIsNotNone(written, "an MPO must be rewritten, not left alone")
        # downscale_stored_image mutates image.image in place (save=False) - the
        # DB row is a separate write the caller makes, so read off this same
        # in-memory instance rather than refresh_from_db(), which would revert
        # to the pre-downscale name after its file was already deleted.
        with image.image.open("rb") as stored:
            result = PILImage.open(stored)
            result.load()
            self.assertEqual(result.format, "JPEG")
            self.assertEqual(getattr(result, "n_frames", 1), 1, "the extra frames carry their own metadata and must not survive")
            self.assertEqual(dict(result.getexif().get_ifd(0x8825)), {})

    def test_the_uploaders_downscale_settings_do_not_matter(self):
        """The strip is unconditional - it is not part of the downscale policy."""
        image = _make_image_row(_mpo_bytes(), name="depth-2.jpg")

        self.assertIsNotNone(downscale_stored_image(image, max_dimension=None, convert_webp=False))
