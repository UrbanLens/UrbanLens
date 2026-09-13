"""Embedded photo keywords must be read in the sandbox, before the upload is rewritten.

``MetadataKeywordProvider.generate`` opened the stored file with Pillow in ``generate_image_keywords``, on the
interactive worker that holds REData and OAuth credentials and has full egress (P116). It also ran after
``process_image_upload`` had rewritten the file, and a rewrite keeps no XMP or IPTC, so a photo carrying an EXIF
block - nearly every camera photo - had lost its keywords before the provider looked.
"""

from __future__ import annotations

import io
import struct
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images import ImageKeyword
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.plugins.builtin.photo_keywords import MetadataKeywordProvider
from urbanlens.dashboard.services.media.images import extract_embedded_keywords
from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image
from urbanlens.dashboard.services.photos.uploads import attach_deduped_copy
from urbanlens.dashboard.services.sandbox.guard import UnsandboxedParseError
from urbanlens.dashboard.tasks import _process_photo_upload

PROVIDERS = "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers"

XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:subject><rdf:Bag>'
    b"<rdf:li>abandoned</rdf:li><rdf:li>sanatorium</rdf:li>"
    b"</rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>"
)


def _camera_jpeg() -> bytes:
    """A photo carrying an EXIF block and XMP keywords, as a camera and then Lightroom leave one."""
    exif = PILImage.Exif()
    exif[0x010F] = "UrbanLens Test Camera"
    buffer = io.BytesIO()
    PILImage.new("RGB", (64, 48), (90, 120, 150)).save(buffer, format="JPEG", exif=exif, xmp=XMP)
    return buffer.getvalue()


def _iptc_jpeg(*keywords: bytes) -> bytes:
    """A JPEG whose only keywords are IPTC 2:25 records in a Photoshop APP13 segment."""
    buffer = io.BytesIO()
    PILImage.new("RGB", (64, 48), (90, 120, 150)).save(buffer, format="JPEG")
    iim = b"".join(b"\x1c\x02\x19" + struct.pack(">H", len(keyword)) + keyword for keyword in keywords)
    resource = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iim)) + iim
    payload = b"Photoshop 3.0\x00" + resource + (b"\x00" if len(iim) % 2 else b"")
    jpeg = buffer.getvalue()
    return jpeg[:2] + b"\xff\xed" + struct.pack(">H", len(payload) + 2) + payload + jpeg[2:]


class EmbeddedKeywordsAreReadInTheSandboxTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.profile.generate_photo_keywords = True
        self.profile.save(update_fields=["generate_photo_keywords"])
        self.image = self._upload(_camera_jpeg())

    def _upload(self, data: bytes) -> Image:
        upload = SimpleUploadedFile("IMG_0001.jpg", data, content_type="image/jpeg")
        return baker.make("dashboard.Image", profile=self.profile, image=upload, pending_scan=False)

    def _process(self, image: Image) -> None:
        """The sandbox half of ``process_image_upload``, persisted the way that task persists it."""
        with override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"):
            result = _process_photo_upload(image, image.pk, strip_location=False)
        assert result is not None
        Image.objects.filter(pk=image.pk).update(**result.update_fields)
        image.refresh_from_db()

    def _stored_keywords(self, image: Image) -> set[str]:
        with mock.patch(PROVIDERS, return_value=[MetadataKeywordProvider()]):
            generate_keywords_for_image(image.pk)
        return set(ImageKeyword.objects.filter(image=image).values_list("keyword", flat=True))

    def test_the_keyword_provider_does_not_decode_the_upload(self) -> None:
        self._process(self.image)

        with mock.patch.object(PILImage, "open", side_effect=AssertionError("the provider decoded the upload")):
            keywords = {result.keyword for result in MetadataKeywordProvider().generate(self.image)}

        self.assertEqual(keywords, {"abandoned", "sanatorium"})

    def test_embedded_keywords_survive_the_upload_being_rewritten(self) -> None:
        self._process(self.image)
        with self.image.image.open("rb") as stored:
            self.assertNotIn(b"sanatorium", stored.read(), "the upload was not rewritten, so this proves nothing")

        self.assertEqual(self._stored_keywords(self.image), {"abandoned", "sanatorium"})

    def test_iptc_keywords_are_read_as_well_as_xmp(self) -> None:
        image = self._upload(_iptc_jpeg(b"asylum", b"Decay"))
        self._process(image)

        self.assertEqual(self._stored_keywords(image), {"asylum", "decay"})

    def test_the_web_process_may_not_read_them(self) -> None:
        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            self.image.image.open("rb") as stored,
            self.assertRaises(UnsandboxedParseError),
        ):
            extract_embedded_keywords(stored)

    def test_a_photo_the_upload_task_never_read_keeps_its_keywords(self) -> None:
        """Rows processed before the field existed have nothing recorded; keywording must not wipe what they had."""
        ImageKeyword.objects.create(image=self.image, source=MetadataKeywordProvider.slug, keyword="hospital")

        self.assertEqual(self._stored_keywords(self.image), {"hospital"})

    def test_a_deduplicated_copy_carries_the_keywords(self) -> None:
        """A dedup sibling never runs the upload task, and the file it shares has already been rewritten."""
        self._process(self.image)

        copy = attach_deduped_copy(self.image, self.profile, self.profile, "")

        copy.refresh_from_db()
        self.assertEqual(copy.embedded_keywords, self.image.embedded_keywords)
