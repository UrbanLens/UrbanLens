"""Every stored photo, and every copy made from one, carries none of the uploader's metadata.

``downscale_stored_image`` rewrote a photo only when it resized, converted, or found an EXIF block, and kept the
original when a rewrite came out larger. A photo whose only metadata was XMP, IPTC, a JPEG comment or PNG text -
XMP can carry GPS - was stored and served exactly as uploaded, and GIF, BMP and animated images were never rewritten
at all. A rewrite did not guarantee a clean file either: Pillow's JPEG and GIF writers copy the source's comment
unless told otherwise, and its TIFF writer copies XMP and IPTC tags (P118).

Each fixture plants one metadata carrier holding a marker unique to it, so a failure names what leaked. The search
covers the raw bytes, every zlib stream decompressed (PNG ``zTXt`` and compressed ``iTXt`` hide text from a plain byte
search), and everything Pillow decodes as metadata on every frame. The ICC colour profile is kept deliberately, so no
fixture carries one.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import io
from itertools import count
from pathlib import Path
from unittest import mock
import zlib

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from PIL import Image as PILImage, PngImagePlugin, TiffImagePlugin

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.media.images import (
    downscale_stored_image,
    write_image_analysis_thumbnail,
    write_image_marker_thumbnail,
    write_image_thumbnail,
)
from urbanlens.dashboard.tasks import _process_photo_upload, process_image_upload

MARK = b"ULMETA"
SANDBOX = {"UL_PROCESS_ROLE": "sandbox", "UL_UNTRUSTED_PARSE_POLICY": "deny"}
ANIMATED = ("gif-animated-comment", "apng-text", "webp-animated-exif-xmp")

_usernames = count()
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".bmp": "image/bmp",
}


def _marker(slug: str) -> str:
    return f"ULMETA-{slug}"


def _canvas(size: tuple[int, int], shade: int = 0) -> PILImage.Image:
    return PILImage.new("RGB", size, (90 + shade, 120, 150 - shade))


def _save(image: PILImage.Image, fmt: str, **params) -> bytes:
    # Pillow leaves encoder state on every frame it saves, which breaks the next fixture sharing those frames.
    if "append_images" in params:
        params["append_images"] = [frame.copy() for frame in params["append_images"]]
    buffer = io.BytesIO()
    image.copy().save(buffer, format=fmt, **params)
    return buffer.getvalue()


def _xmp(slug: str) -> bytes:
    return (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:description="'
        + _marker(slug).encode()
        + b'"/></rdf:RDF></x:xmpmeta>'
    )


def _exif(slug: str, tag: int = 0x010F, ifd: int | None = None) -> PILImage.Exif:
    exif = PILImage.Exif()
    if ifd == 0x8825:
        gps = exif.get_ifd(ifd)
        gps[1], gps[2], gps[0x1D] = "N", (51.0, 30.0, 0.0), _marker(slug)
    elif ifd is not None:
        exif.get_ifd(ifd)[tag] = b"ASCII\0\0\0" + _marker(slug).encode()
    else:
        exif[tag] = _marker(slug)
    return exif


def _png_text(slug: str, *, compressed: bool = False, international: bool = False) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    if international:
        info.add_itxt("Description", _marker(slug), zip=compressed)
    else:
        info.add_text("Comment", _marker(slug), zip=compressed)
    return info


def _jpeg_with_iptc(size: tuple[int, int], slug: str) -> bytes:
    data = _save(_canvas(size), "JPEG")
    payload = _marker(slug).encode()
    iptc = b"\x1c\x02\x78" + len(payload).to_bytes(2, "big") + payload
    resource = (
        b"8BIM" + (0x0404).to_bytes(2, "big") + b"\0\0" + len(iptc).to_bytes(4, "big") + iptc + b"\0" * (len(iptc) % 2)
    )
    segment = b"Photoshop 3.0\0" + resource
    return data[:2] + b"\xff\xed" + (len(segment) + 2).to_bytes(2, "big") + segment + data[2:]


def _gif_with_xmp(size: tuple[int, int], slug: str) -> bytes:
    data = _save(_canvas(size), "GIF")
    payload = _xmp(slug)
    blocks = b"".join(bytes([len(payload[i : i + 255])]) + payload[i : i + 255] for i in range(0, len(payload), 255))
    return data[:-1] + b"\x21\xff\x0bXMP DataXMP" + blocks + b"\0" + data[-1:]


def _tiff_tag(size: tuple[int, int], slug: str, tag: int, as_bytes: bool = False) -> bytes:
    info = TiffImagePlugin.ImageFileDirectory_v2()
    info[tag] = _marker(slug).encode() if as_bytes else _marker(slug)
    return _save(_canvas(size), "TIFF", tiffinfo=info)


def _frames(size: tuple[int, int]) -> list[PILImage.Image]:
    return [_canvas(size, shade=index * 40) for index in range(3)]


def _fixtures(size: tuple[int, int] = (48, 32)) -> dict[str, tuple[str, bytes]]:
    """Every fixture, as ``slug -> (file name, bytes)``; each carries ``_marker(slug)`` in one metadata carrier."""
    canvas = _canvas(size)
    frames = _frames(size)
    builders: dict[str, tuple[str, Callable[[str], bytes]]] = {
        "jpeg-exif-make": ("a.jpg", lambda s: _save(canvas, "JPEG", exif=_exif(s))),
        "jpeg-exif-gps": ("a.jpg", lambda s: _save(canvas, "JPEG", exif=_exif(s, ifd=0x8825))),
        "jpeg-exif-usercomment": ("a.jpg", lambda s: _save(canvas, "JPEG", exif=_exif(s, 0x9286, ifd=0x8769))),
        "jpeg-xmp": ("a.jpg", lambda s: _save(canvas, "JPEG", xmp=_xmp(s))),
        "jpeg-comment": ("a.jpg", lambda s: _save(canvas, "JPEG", comment=_marker(s).encode())),
        "jpeg-iptc": ("a.jpg", lambda s: _jpeg_with_iptc(size, s)),
        "jpeg-trailing-bytes": ("a.jpg", lambda s: _save(canvas, "JPEG") + _marker(s).encode()),
        "mpo-exif": ("a.jpg", lambda s: _save(canvas, "MPO", save_all=True, append_images=frames[1:2], exif=_exif(s))),
        "png-text": ("a.png", lambda s: _save(canvas, "PNG", pnginfo=_png_text(s))),
        "png-ztxt": ("a.png", lambda s: _save(canvas, "PNG", pnginfo=_png_text(s, compressed=True))),
        "png-itxt-compressed": (
            "a.png",
            lambda s: _save(canvas, "PNG", pnginfo=_png_text(s, compressed=True, international=True)),
        ),
        "png-xmp": ("a.png", lambda s: _save(canvas, "PNG", pnginfo=_png_xmp(s))),
        "png-exif": ("a.png", lambda s: _save(canvas, "PNG", exif=_exif(s))),
        "png-trailing-bytes": ("a.png", lambda s: _save(canvas, "PNG") + _marker(s).encode()),
        "apng-text": (
            "a.png",
            lambda s: _save(frames[0], "PNG", save_all=True, append_images=frames[1:], pnginfo=_png_text(s)),
        ),
        "webp-exif": ("a.webp", lambda s: _save(canvas, "WEBP", exif=_exif(s))),
        "webp-xmp": ("a.webp", lambda s: _save(canvas, "WEBP", xmp=_xmp(s))),
        "webp-animated-exif-xmp": (
            "a.webp",
            lambda s: _save(frames[0], "WEBP", save_all=True, append_images=frames[1:], exif=_exif(s), xmp=_xmp(s)),
        ),
        "gif-comment": ("a.gif", lambda s: _save(canvas, "GIF", comment=_marker(s).encode())),
        "gif-animated-comment": (
            "a.gif",
            lambda s: _save(
                frames[0],
                "GIF",
                save_all=True,
                append_images=frames[1:],
                duration=80,
                loop=0,
                comment=_marker(s).encode(),
            ),
        ),
        "gif-xmp": ("a.gif", lambda s: _gif_with_xmp(size, s)),
        "tiff-description": ("a.tif", lambda s: _tiff_tag(size, s, 270)),
        "tiff-artist": ("a.tif", lambda s: _tiff_tag(size, s, 315)),
        "tiff-copyright": ("a.tif", lambda s: _tiff_tag(size, s, 33432)),
        "tiff-xmp": ("a.tif", lambda s: _tiff_tag(size, s, 700, as_bytes=True)),
        "tiff-iptc": ("a.tif", lambda s: _tiff_tag(size, s, 33723, as_bytes=True)),
        "tiff-photoshop": ("a.tif", lambda s: _tiff_tag(size, s, 34377, as_bytes=True)),
        "tiff-exif": ("a.tif", lambda s: _save(canvas, "TIFF", exif=_exif(s))),
        "tiff-every-page": (
            "a.tif",
            lambda s: _save(frames[0], "TIFF", save_all=True, append_images=frames[1:], description=_marker(s)),
        ),
        "avif-exif": ("a.avif", lambda s: _save(canvas, "AVIF", exif=_exif(s))),
        "avif-xmp": ("a.avif", lambda s: _save(canvas, "AVIF", xmp=_xmp(s))),
        "heif-exif": ("a.heic", lambda s: _save(canvas, "HEIF", exif=_exif(s).tobytes())),
        "heif-xmp": ("a.heic", lambda s: _save(canvas, "HEIF", xmp=_xmp(s))),
        "bmp-trailing-bytes": ("a.bmp", lambda s: _save(canvas, "BMP") + _marker(s).encode()),
    }
    return {slug: (name, build(slug)) for slug, (name, build) in builders.items()}


def _png_xmp(slug: str) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", _xmp(slug).decode())
    return info


def _everything_readable(data: bytes) -> bytes:
    """The file's bytes, every zlib stream in it decompressed, and what Pillow decodes as metadata on every frame."""
    parts = [data]
    view = memoryview(data)
    for start in range(len(data) - 1):
        if data[start] == 0x78 and ((data[start] << 8) | data[start + 1]) % 31 == 0:
            with contextlib.suppress(zlib.error):
                parts.append(zlib.decompressobj().decompress(view[start:], 1 << 20))
    with contextlib.suppress(Exception):
        image = PILImage.open(io.BytesIO(data))
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            exif = image.getexif()
            parts.extend(
                repr(value).encode() for value in (image.info, dict(exif), exif.get_ifd(0x8769), exif.get_ifd(0x8825))
            )
            parts.append(repr(getattr(image, "text", {})).encode())
            with contextlib.suppress(Exception):
                parts.append(repr(image.getxmp()).encode())
    return b"".join(parts)


def _body(response) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else response.content


class _MetadataCase(TestCase):
    def _row(self, name: str, data: bytes, *, pending_scan: bool = False) -> Image:
        profile = User.objects.create(username=f"metadata{next(_usernames)}").profile
        upload = SimpleUploadedFile(name, data, content_type="application/octet-stream")
        return Image.objects.create(
            image=upload, profile=profile, media_type=MediaKind.PHOTO, pending_scan=pending_scan
        )

    def _read(self, field) -> bytes:
        with field.open("rb") as handle:
            return handle.read()

    def assertClean(self, data: bytes, where: str) -> None:
        readable = _everything_readable(data)
        at = readable.find(MARK)
        self.assertEqual(at, -1, f"{where} still carries {readable[max(at - 40, 0) : at + 60]!r}")


class TheDetectorSeesEveryCarrierTests(_MetadataCase):
    """Guards everything below: a carrier the search cannot see would pass every test without proving anything."""

    def test_every_fixture_carries_its_own_marker(self) -> None:
        for slug, (_, data) in _fixtures().items():
            with self.subTest(slug):
                self.assertIn(_marker(slug).encode(), _everything_readable(data))

    def test_a_compressed_marker_is_found_only_by_decompressing(self) -> None:
        for slug in ("png-ztxt", "png-itxt-compressed"):
            with self.subTest(slug):
                data = _fixtures()[slug][1]
                self.assertNotIn(_marker(slug).encode(), data)
                self.assertIn(_marker(slug).encode(), _everything_readable(data))

    def test_the_animated_fixtures_have_every_frame(self) -> None:
        fixtures = _fixtures()
        for slug in ANIMATED:
            with self.subTest(slug):
                self.assertEqual(getattr(PILImage.open(io.BytesIO(fixtures[slug][1])), "n_frames", 1), 3)


class TheStoredFileTests(_MetadataCase):
    def test_no_metadata_survives_the_reencode(self) -> None:
        for convert_webp in (False, True):
            for max_dimension in (None, 16):
                for slug, (name, data) in _fixtures().items():
                    with self.subTest(slug, convert_webp=convert_webp, max_dimension=max_dimension):
                        row = self._row(name, data)

                        with override_settings(**SANDBOX):
                            self.assertIsNotNone(downscale_stored_image(row, max_dimension, convert_webp))

                        stored = self._read(row.image)
                        self.assertClean(stored, "the stored file")
                        PILImage.open(io.BytesIO(stored)).load()

    def test_a_photo_with_nothing_to_strip_is_still_pipeline_output(self) -> None:
        for name, data in (
            ("plain.jpg", _save(_canvas((48, 32)), "JPEG")),
            ("plain.bmp", _save(_canvas((48, 32)), "BMP")),
        ):
            with self.subTest(name):
                row = self._row(name, data)
                original = row.image.name

                with override_settings(**SANDBOX):
                    self.assertIsNotNone(downscale_stored_image(row, max_dimension=None, convert_webp=False))

                self.assertNotEqual(row.image.name, original)

    def test_an_animation_stays_animated(self) -> None:
        fixtures = _fixtures()
        expected = {"gif-animated-comment": "GIF", "apng-text": "PNG", "webp-animated-exif-xmp": "WEBP"}
        for slug in ANIMATED:
            for convert_webp in (False, True):
                with self.subTest(slug, convert_webp=convert_webp):
                    row = self._row(*fixtures[slug])

                    with override_settings(**SANDBOX):
                        downscale_stored_image(row, max_dimension=None, convert_webp=convert_webp)

                    reopened = PILImage.open(io.BytesIO(self._read(row.image)))
                    self.assertEqual(
                        (reopened.format, getattr(reopened, "n_frames", 1)),
                        ("WEBP" if convert_webp else expected[slug], 3),
                    )


class TheUploadTaskTests(_MetadataCase):
    def test_the_stored_file_and_every_thumbnail_are_clean(self) -> None:
        for convert_webp in (False, True):
            for slug, (name, data) in _fixtures().items():
                with self.subTest(slug, convert_webp=convert_webp):
                    row = self._row(name, data, pending_scan=True)

                    with (
                        override_settings(**SANDBOX),
                        mock.patch(
                            "urbanlens.dashboard.services.media.storage.get_stored_photo_policy",
                            return_value=(None, convert_webp),
                        ),
                    ):
                        result = _process_photo_upload(row, row.pk, strip_location=False)

                    assert result is not None
                    self.assertIn("image", result.update_fields)
                    for field in (row.image, row.thumbnail, row.marker_thumbnail, row.analysis_thumbnail):
                        self.assertClean(self._read(field), field.field.name)

    def test_a_pending_upload_that_cannot_be_reencoded_is_reported_as_unprocessable(self) -> None:
        name, data = _fixtures()["jpeg-xmp"]
        row = self._row(name, data, pending_scan=True)

        with (
            override_settings(**SANDBOX),
            mock.patch("urbanlens.dashboard.services.media.images.downscale_stored_image", side_effect=OSError("boom")),
        ):
            self.assertIsNone(_process_photo_upload(row, row.pk, strip_location=False))


class CopiesMadeFromAFileStoredBeforeThePipelineTests(_MetadataCase):
    """A file stored before every photo was re-encoded is not clean yet, so nothing made from it may inherit that."""

    def test_every_thumbnail_is_clean(self) -> None:
        writers = {
            "thumbnail": write_image_thumbnail,
            "marker_thumbnail": write_image_marker_thumbnail,
            "analysis_thumbnail": write_image_analysis_thumbnail,
        }
        for slug, (name, data) in _fixtures().items():
            row = self._row(name, b"")
            row.image.save(name, ContentFile(data), save=True)
            for field_name, write in writers.items():
                with self.subTest(slug, copy=field_name):
                    with override_settings(**SANDBOX):
                        self.assertTrue(write(row, force=True))

                    self.assertClean(self._read(getattr(row, field_name)), field_name)

    def test_a_preview_is_clean(self) -> None:
        from urbanlens.dashboard.services.media.previews import render_preview

        for slug, (_, data) in _fixtures().items():
            with self.subTest(slug):
                with override_settings(**SANDBOX):
                    rendered = render_preview(data)

                assert rendered is not None
                self.assertClean(rendered[0], "the preview")

    def test_a_shrunk_label_icon_is_clean(self) -> None:
        from urbanlens.dashboard.services.labels.icons import shrink_icon

        for slug, (name, data) in _fixtures((300, 200)).items():
            with self.subTest(slug):
                with override_settings(**SANDBOX):
                    shrunk = shrink_icon(io.BytesIO(data), name)

                assert shrunk is not None
                self.assertClean(shrunk[0], "the icon")


class APhotoUploadedThroughTheSiteTests(_MetadataCase):
    """The whole path: the upload endpoint, the upload task, and the bytes the media gate serves."""

    def test_nothing_the_site_serves_carries_metadata(self) -> None:
        User.objects.create(username="bootstrap-admin")
        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                user = User.objects.create(username=f"uploader{next(_usernames)}")
                client = Client()
                client.force_login(user)

                upload = SimpleUploadedFile(name, data, content_type=_CONTENT_TYPES[Path(name).suffix])
                response = client.post(reverse("vault.photos.upload"), {"image": upload})
                self.assertEqual(response.status_code, 201, response.content)
                row = Image.objects.get(profile__user=user)
                with override_settings(**SANDBOX):
                    process_image_upload.apply(args=(row.pk,))

                row.refresh_from_db()
                self.assertFalse(row.pending_scan)
                with override_settings(MEDIA_X_ACCEL=False):
                    for field in (row.image, row.thumbnail, row.marker_thumbnail):
                        served = client.get(reverse("media", args=[field.name]))
                        self.assertEqual(served.status_code, 200, field.name)
                        self.assertClean(_body(served), f"the served {field.field.name}")
