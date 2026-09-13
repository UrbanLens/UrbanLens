"""Every stored photo is pipeline output, with no metadata left in the file.

``downscale_stored_image`` rewrote a photo only when it resized, converted, or found an EXIF block, and kept the
original when a rewrite came out larger. A photo whose only metadata was XMP, IPTC, a JPEG comment or PNG text -
XMP can carry GPS - was stored and served exactly as uploaded, and GIF, BMP and animated images were never rewritten
at all. A rewrite did not guarantee a clean file either: Pillow's JPEG and GIF writers copy the source's comment
unless told otherwise, and its TIFF writer copies XMP and IPTC tags (P118).
"""

from __future__ import annotations

import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage, PngImagePlugin

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.media.images import downscale_stored_image, write_image_analysis_thumbnail
from urbanlens.dashboard.tasks import _process_photo_upload

MARKERS = (b"SECRETCAM", b"51,30.0N", b"SECRETNOTE")
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" exif:GPSLatitude="51,30.0N"/>'
    b"</rdf:RDF></x:xmpmeta>"
)
METADATA_INFO_KEYS = {"exif", "xmp", "comment", "photoshop", "Comment", "XML:com.adobe.xmp"}
SANDBOX = {"UL_PROCESS_ROLE": "sandbox", "UL_UNTRUSTED_PARSE_POLICY": "deny"}
#: Tags a TIFF needs to describe its own pixels, which getexif() reports for every TIFF.
TIFF_STRUCTURE_TAGS = {254, 256, 257, 258, 259, 262, 273, 277, 278, 279, 282, 283, 284, 296, 317, 320, 338, 339}


def _encode(fmt: str, image: PILImage.Image | None = None, **params) -> bytes:
    buffer = io.BytesIO()
    (image or PILImage.new("RGB", (48, 32), (90, 120, 150))).save(buffer, format=fmt, **params)
    return buffer.getvalue()


def _animated_gif() -> bytes:
    frames = [PILImage.new("RGB", (48, 32), (index * 90, 40, 200 - index * 60)) for index in range(3)]
    return _encode(
        "GIF", frames[0], save_all=True, append_images=frames[1:], duration=80, loop=0, comment=b"SECRETNOTE"
    )


def _png_with_text() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "SECRETNOTE")
    info.add_itxt("XML:com.adobe.xmp", XMP.decode())
    return _encode("PNG", pnginfo=info)


def _fixtures() -> dict[str, tuple[str, bytes]]:
    return {
        "jpeg with only xmp": ("xmp.jpg", _encode("JPEG", xmp=XMP)),
        "jpeg with only a comment": ("comment.jpg", _encode("JPEG", comment=b"SECRETNOTE")),
        "png with text chunks": ("text.png", _png_with_text()),
        "webp with only xmp": ("xmp.webp", _encode("WEBP", xmp=XMP)),
        "gif with a comment": ("comment.gif", _encode("GIF", PILImage.new("P", (48, 32)), comment=b"SECRETNOTE")),
        "animated gif with a comment": ("animated.gif", _animated_gif()),
        "tiff with xmp and a description": ("tags.tif", _encode("TIFF", tiffinfo={270: "SECRETNOTE", 700: XMP})),
        "avif with exif": ("exif.avif", _encode("AVIF", exif=_camera_exif())),
    }


def _camera_exif() -> PILImage.Exif:
    exif = PILImage.Exif()
    exif[0x010F] = "SECRETCAM"
    return exif


class _StoredPhotoCase(TestCase):
    def _row(self, name: str, data: bytes, *, pending_scan: bool = False) -> Image:
        profile = User.objects.create(username=f"u{Image.objects.count()}").profile
        upload = SimpleUploadedFile(name, data, content_type="application/octet-stream")
        return Image.objects.create(
            image=upload, profile=profile, media_type=MediaKind.PHOTO, pending_scan=pending_scan
        )

    def _stored(self, row: Image) -> bytes:
        with row.image.open("rb") as handle:
            return handle.read()

    def assertCarriesNoMetadata(self, data: bytes) -> None:
        for marker in MARKERS:
            self.assertNotIn(marker, data)
        reopened = PILImage.open(io.BytesIO(data))
        self.assertEqual(METADATA_INFO_KEYS & set(reopened.info), set())
        self.assertEqual(set(reopened.getexif()) - TIFF_STRUCTURE_TAGS, set())


class TheFixturesCarryMetadataTests(_StoredPhotoCase):
    def test_every_fixture_carries_a_marker(self) -> None:
        """Guards the tests below: a fixture with nothing in it would pass them without proving anything."""
        for label, (_, data) in _fixtures().items():
            with self.subTest(label):
                self.assertTrue(any(marker in data for marker in MARKERS))

    def test_the_animation_fixture_has_every_frame(self) -> None:
        self.assertEqual(PILImage.open(io.BytesIO(_animated_gif())).n_frames, 3)


class EveryStoredPhotoIsReencodedTests(_StoredPhotoCase):
    def test_no_metadata_survives_in_the_stored_file(self) -> None:
        for convert_webp in (False, True):
            for label, (name, data) in _fixtures().items():
                with self.subTest(label, convert_webp=convert_webp):
                    row = self._row(name, data)

                    self.assertIsNotNone(downscale_stored_image(row, max_dimension=None, convert_webp=convert_webp))

                    self.assertCarriesNoMetadata(self._stored(row))

    def test_a_photo_with_nothing_to_strip_is_still_pipeline_output(self) -> None:
        for name, data in (("plain.jpg", _encode("JPEG")), ("plain.bmp", _encode("BMP"))):
            with self.subTest(name):
                row = self._row(name, data)
                original = row.image.name

                self.assertIsNotNone(downscale_stored_image(row, max_dimension=None, convert_webp=False))

                self.assertNotEqual(row.image.name, original)

    def test_an_animation_stays_animated(self) -> None:
        for convert_webp, expected_format in ((False, "GIF"), (True, "WEBP")):
            with self.subTest(expected_format):
                row = self._row("animated.gif", _animated_gif())

                downscale_stored_image(row, max_dimension=None, convert_webp=convert_webp)

                reopened = PILImage.open(io.BytesIO(self._stored(row)))
                self.assertEqual((reopened.format, reopened.n_frames), (expected_format, 3))

    def test_the_upload_task_stores_a_clean_file(self) -> None:
        row = self._row("xmp.jpg", _encode("JPEG", xmp=XMP))

        with override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"):
            result = _process_photo_upload(row, row.pk, strip_location=False)

        assert result is not None
        self.assertIn("image", result.update_fields)
        self.assertCarriesNoMetadata(self._stored(row))


class AFreshUploadThatCannotBeReencodedIsNotKeptTests(_StoredPhotoCase):
    def test_a_pending_upload_is_reported_as_unprocessable(self) -> None:
        row = self._row("xmp.jpg", _encode("JPEG", xmp=XMP), pending_scan=True)

        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch("urbanlens.dashboard.services.media.images.downscale_stored_image", side_effect=OSError("boom")),
        ):
            self.assertIsNone(_process_photo_upload(row, row.pk, strip_location=False))


class DerivedCopiesCarryNoMetadataTests(_StoredPhotoCase):
    def test_the_analysis_copy_of_a_file_stored_before_the_pipeline_is_clean(self) -> None:
        """The analysis JPEG goes to an outside AI provider, and a stored file from before this fix is not clean yet."""
        row = self._row("legacy.jpg", b"")
        row.image.save("legacy.jpg", ContentFile(_encode("JPEG", comment=b"SECRETNOTE")), save=True)

        self.assertTrue(write_image_analysis_thumbnail(row, force=True))

        with row.analysis_thumbnail.open("rb") as handle:
            self.assertCarriesNoMetadata(handle.read())

    def test_a_preview_is_clean(self) -> None:
        from urbanlens.dashboard.services.media.previews import render_preview

        with override_settings(**SANDBOX):
            rendered = render_preview(
                _encode("JPEG", PILImage.new("RGB", (600, 400)), comment=b"SECRETNOTE"), "image/jpeg"
            )

        assert rendered is not None
        self.assertCarriesNoMetadata(rendered[0])

    def test_a_shrunk_label_icon_is_clean(self) -> None:
        from urbanlens.dashboard.services.labels.icons import shrink_icon

        with override_settings(**SANDBOX):
            shrunk = shrink_icon(
                io.BytesIO(_encode("JPEG", PILImage.new("RGB", (600, 400)), comment=b"SECRETNOTE")), "i.jpg"
            )

        assert shrunk is not None
        self.assertCarriesNoMetadata(shrunk[0])
