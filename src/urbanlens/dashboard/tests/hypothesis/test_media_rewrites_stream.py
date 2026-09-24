"""A rewritten video, a converted document and an OCR pass never hold the whole file in memory.

Uploads may be hundreds of megabytes, and these run in a media worker beside other jobs. Each one
already works on a temporary copy on disk; the property pinned here is that the result goes back to
storage, and the PDF goes to the rasteriser, from that path rather than as one ``bytes``.
"""

from __future__ import annotations

import os
import tracemalloc
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.documents import convert_to_pdf, extract_pdf_text
from urbanlens.dashboard.services.media.videos import process_uploaded_video

FILE_BYTES = 16 * 1024 * 1024
#: Well under the file, well over what chunked copying and the bookkeeping around it allocate.
PEAK_CEILING = FILE_BYTES // 2


def _write_large(path: str) -> None:
    with open(path, "wb") as out:
        chunk = b"x" * (1024 * 1024)
        out.writelines(chunk for _ in range(FILE_BYTES // len(chunk)))


def _peak_while(call):
    tracemalloc.start()
    try:
        result = call()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


class VideoRewriteStreamsTests(TestCase):
    def test_a_rewritten_video_is_stored_from_its_path(self) -> None:
        image = baker.make(
            Image,
            profile=baker.make(User).profile,
            image=SimpleUploadedFile("clip.mp4", b"fake-video-bytes", content_type="video/mp4"),
        )

        def remux(_src: str, out_path: str) -> bool:
            _write_large(out_path)
            return True

        with (
            patch("urbanlens.dashboard.services.media.videos.ffmpeg_available", return_value=True),
            patch(
                "urbanlens.dashboard.services.media.videos.extract_video_metadata",
                return_value={"height": 720, "has_location_tag": True},
            ),
            patch("urbanlens.dashboard.services.media.videos._remux_without_location", side_effect=remux),
        ):
            (_metadata, replacement), peak = _peak_while(lambda: process_uploaded_video(image, 1080))

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.size, FILE_BYTES)
        self.assertEqual(image.image.size, FILE_BYTES)
        self.assertLess(peak, PEAK_CEILING, f"peaked at {peak} bytes rewriting a {FILE_BYTES}-byte video")


class DocumentConversionStreamsTests(TestCase):
    def test_a_converted_pdf_is_stored_from_its_path(self) -> None:
        image = baker.make(
            Image,
            profile=baker.make(User).profile,
            image=SimpleUploadedFile("notes.docx", b"docx-bytes", content_type="application/octet-stream"),
        )

        def soffice(args, **_kwargs):
            _write_large(os.path.join(args[args.index("--outdir") + 1], "source.pdf"))
            return MagicMock(returncode=0)

        with (
            patch("urbanlens.dashboard.services.media.documents.soffice_path", return_value="/usr/bin/soffice"),
            patch("urbanlens.dashboard.services.media.documents.subprocess.run", side_effect=soffice),
        ):
            replacement, peak = _peak_while(lambda: convert_to_pdf(image))

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.size, FILE_BYTES)
        self.assertTrue(image.image.name.endswith(".pdf"))
        self.assertLess(peak, PEAK_CEILING, f"peaked at {peak} bytes converting to a {FILE_BYTES}-byte PDF")


class OcrReadsFromAPathTests(TestCase):
    def test_ocr_rasterises_the_pdf_from_a_path(self) -> None:
        image = baker.make(
            Image,
            profile=baker.make(User).profile,
            image=SimpleUploadedFile("scan.pdf", b"%PDF-1.4" + b"x" * FILE_BYTES, content_type="application/pdf"),
        )
        no_native_text = MagicMock()
        no_native_text.pages = [MagicMock(extract_text=MagicMock(return_value=""))]

        with (
            patch("pypdf.PdfReader", return_value=no_native_text),
            patch("shutil.which", return_value="/usr/bin/tesseract"),
            patch("pdf2image.convert_from_path", return_value=["page-1.png"]) as convert,
            patch("pytesseract.image_to_string", return_value="scanned words"),
        ):
            text, peak = _peak_while(lambda: extract_pdf_text(image))

        self.assertEqual(text, "scanned words")
        self.assertTrue(os.path.basename(convert.call_args.args[0]).endswith(".pdf"))
        self.assertLess(peak, PEAK_CEILING, f"peaked at {peak} bytes OCRing a {FILE_BYTES}-byte PDF")
