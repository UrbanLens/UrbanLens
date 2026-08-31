"""Tests for the byte-level metadata stripper.

``services.media.metadata_strip`` rewrites a container by walking its segments
rather than decoding the image, so it is cheap enough to run inside a request -
which is the point, since the alternative is writing the raw upload to
``MEDIA_ROOT`` and cleaning it up after a Celery re-encode.

That makes two properties load-bearing, and both are asserted for every format
it handles:

- the metadata really is gone (the whole reason to run it), and
- the result is still a valid image with the same pixels (it is what gets
  stored and served, and nothing decodes it first to find out).

Anything it does not handle must return ``None``, meaning "leave it to the
re-encode" - never a partially-rewritten file.
"""

from __future__ import annotations

from fractions import Fraction
import io

from hypothesis import given, settings as hypothesis_settings, strategies as st
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.media.metadata_strip import strip_metadata

_GPS_IFD = 0x8825
_MAKE_TAG = 0x010F
_DESCRIPTION_TAG = 0x010E


def _exif() -> PILImage.Exif:
    """An EXIF block carrying a GPS position and identifying text."""
    exif = PILImage.Exif()
    exif[_MAKE_TAG] = "A Photographer"
    exif[_DESCRIPTION_TAG] = "Taken at the old sanatorium"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    gps[2] = (Fraction(42), Fraction(39), Fraction(9))
    gps[3] = "W"
    gps[4] = (Fraction(73), Fraction(45), Fraction(22))
    return exif


def _image(width: int = 32, height: int = 24) -> PILImage.Image:
    """A small image with a recognisable, non-uniform pixel pattern."""
    img = PILImage.new("RGB", (width, height))
    img.putdata([((x * 7) % 256, (y * 11) % 256, (x + y) % 256) for y in range(height) for x in range(width)])
    return img


def _encoded(fmt: str, **save_kwargs: object) -> bytes:
    buf = io.BytesIO()
    _image().save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def _metadata_of(data: bytes) -> tuple[dict, str]:
    """Return (exif tags, all decoded info values) for a stripped file."""
    opened = PILImage.open(io.BytesIO(data))
    opened.load()
    exif = opened.getexif()
    tags = dict(exif)
    tags.update({f"gps{k}": v for k, v in exif.get_ifd(_GPS_IFD).items()})
    info = " ".join(str(value) for key, value in opened.info.items() if key not in ("icc_profile",))
    return tags, info


class JpegStripTests(SimpleTestCase):
    def setUp(self):
        self.original = _encoded("JPEG", exif=_exif(), quality=95)

    def test_the_fixture_carries_what_we_claim_to_remove(self):
        tags, _info = _metadata_of(self.original)

        self.assertIn(_MAKE_TAG, tags)
        self.assertIn("gps2", tags)

    def test_gps_and_identifying_text_are_gone(self):
        stripped = strip_metadata(self.original)

        self.assertIsNotNone(stripped)
        tags, info = _metadata_of(stripped)
        self.assertEqual(tags, {})
        self.assertNotIn("Photographer", info)
        self.assertNotIn("sanatorium", info)

    def test_the_image_still_decodes_to_the_same_pixels(self):
        stripped = strip_metadata(self.original)

        before = PILImage.open(io.BytesIO(self.original))
        after = PILImage.open(io.BytesIO(stripped))
        self.assertEqual(after.format, "JPEG")
        self.assertEqual(after.size, before.size)
        self.assertEqual(list(after.convert("RGB").getdata()), list(before.convert("RGB").getdata()), "the strip must not touch pixel data")

    def test_a_comment_segment_is_dropped(self):
        with_comment = _encoded("JPEG", comment=b"private note about this place", quality=95)

        stripped = strip_metadata(with_comment)

        self.assertNotIn(b"private note", stripped)

    def test_the_colour_profile_survives(self):
        """Dropping APP2 would shift the colours of a wide-gamut photo."""
        profile = b"\x00" * 128
        tagged = _encoded("JPEG", exif=_exif(), icc_profile=profile, quality=95)

        stripped = strip_metadata(tagged)

        self.assertEqual(PILImage.open(io.BytesIO(stripped)).info.get("icc_profile"), profile)


class PngStripTests(SimpleTestCase):
    def _with_text(self) -> bytes:
        from PIL.PngImagePlugin import PngInfo

        meta = PngInfo()
        meta.add_text("Author", "A Photographer")
        meta.add_text("Comment", "Taken at the old sanatorium")
        buf = io.BytesIO()
        _image().save(buf, format="PNG", pnginfo=meta)
        return buf.getvalue()

    def test_the_fixture_carries_what_we_claim_to_remove(self):
        self.assertIn(b"A Photographer", self._with_text())

    def test_text_chunks_are_gone_and_the_image_survives(self):
        original = self._with_text()

        stripped = strip_metadata(original)

        self.assertIsNotNone(stripped)
        self.assertNotIn(b"A Photographer", stripped)
        self.assertNotIn(b"sanatorium", stripped)
        after = PILImage.open(io.BytesIO(stripped))
        self.assertEqual(after.format, "PNG")
        self.assertEqual(list(after.convert("RGB").getdata()), list(_image().getdata()))


class WebpStripTests(SimpleTestCase):
    def test_exif_is_gone_and_the_image_survives(self):
        original = _encoded("WEBP", exif=_exif(), lossless=True)

        stripped = strip_metadata(original)

        self.assertIsNotNone(stripped)
        tags, _info = _metadata_of(stripped)
        self.assertEqual(tags, {})
        after = PILImage.open(io.BytesIO(stripped))
        self.assertEqual(after.format, "WEBP")
        self.assertEqual(after.size, (32, 24))

    def test_a_plain_webp_without_metadata_still_decodes(self):
        original = _encoded("WEBP", lossless=True)

        stripped = strip_metadata(original)

        self.assertIsNotNone(stripped)
        after = PILImage.open(io.BytesIO(stripped))
        self.assertEqual(list(after.convert("RGB").getdata()), list(_image().getdata()))


class UnhandledInputTests(SimpleTestCase):
    """Anything it cannot rewrite must say so rather than guess."""

    def test_formats_it_does_not_handle_return_none(self):
        for fmt in ("GIF", "TIFF", "BMP"):
            with self.subTest(fmt=fmt):
                self.assertIsNone(strip_metadata(_encoded(fmt)))

    def test_empty_input_returns_none(self):
        self.assertIsNone(strip_metadata(b""))

    @given(data=st.binary(min_size=0, max_size=400))
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_arbitrary_bytes_never_raise(self, data: bytes):
        """A malformed upload must degrade to None, not to a 500."""
        result = strip_metadata(data)

        self.assertTrue(result is None or isinstance(result, bytes))

    @given(prefix=st.sampled_from([b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00WEBP"]), tail=st.binary(min_size=0, max_size=200))
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_truncated_containers_never_raise(self, prefix: bytes, tail: bytes):
        """Fuzzed headers reach the per-format parsers rather than the type sniff."""
        result = strip_metadata(prefix + tail)

        self.assertTrue(result is None or isinstance(result, bytes))
