"""Unit tests for services.media.documents - LibreOffice/pypdf/OCR-backed document processing.

External binaries (soffice/tesseract) and libraries (pypdf/pdf2image/pytesseract)
are mocked throughout: these tests verify the Python-side decision logic, not
the actual conversion/OCR, which needs Docker.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.documents import (
    _OCR_MAX_CHARS,
    _OCR_MAX_PAGES,
    _OCR_MAX_PIXELS,
    CONVERTIBLE_DOCUMENT_EXTENSIONS,
    convert_to_pdf,
    extract_pdf_text,
    soffice_available,
)


class SofficeAvailableTests(SimpleTestCase):
    def test_true_when_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/soffice"):
            self.assertTrue(soffice_available())

    def test_false_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            self.assertFalse(soffice_available())


class ConvertToPdfTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)

    def test_already_pdf_is_left_alone(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("report.pdf", b"%PDF-1.4", content_type="application/pdf"),
        )
        self.assertIsNone(convert_to_pdf(image))

    def test_unsupported_extension_is_left_alone(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("photo.jpg", b"jpeg-bytes", content_type="image/jpeg"),
        )
        self.assertIsNone(convert_to_pdf(image))

    def test_no_soffice_binary_skips_conversion(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile(
                "notes.docx",
                b"doc-bytes",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )
        with patch("urbanlens.dashboard.services.media.documents.soffice_path", return_value=None):
            self.assertIsNone(convert_to_pdf(image))

    def test_conversion_failure_returns_none(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile(
                "notes.docx",
                b"doc-bytes",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )
        with (
            patch("urbanlens.dashboard.services.media.documents.soffice_path", return_value="/usr/bin/soffice"),
            patch("subprocess.run", side_effect=subprocess.SubprocessError("boom")),
        ):
            self.assertIsNone(convert_to_pdf(image))

    def test_all_convertible_extensions_recognized(self) -> None:
        self.assertIn(".docx", CONVERTIBLE_DOCUMENT_EXTENSIONS)
        self.assertIn(".txt", CONVERTIBLE_DOCUMENT_EXTENSIONS)
        self.assertNotIn(".pdf", CONVERTIBLE_DOCUMENT_EXTENSIONS)


class ExtractPdfTextTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("report.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
        )

    def test_non_pdf_returns_none(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("photo.jpg", b"jpeg-bytes", content_type="image/jpeg"),
        )
        self.assertIsNone(extract_pdf_text(image))

    def test_native_text_layer_used_when_present(self) -> None:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Hello from page one"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        with patch("pypdf.PdfReader", return_value=mock_reader):
            text = extract_pdf_text(self.image)
        self.assertEqual(text, "Hello from page one")

    def test_no_text_and_no_tesseract_returns_none(self) -> None:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        with (
            patch("pypdf.PdfReader", return_value=mock_reader),
            patch("shutil.which", return_value=None),
        ):
            self.assertIsNone(extract_pdf_text(self.image))

    def test_ocr_fallback_used_when_no_native_text(self) -> None:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        with (
            patch("pypdf.PdfReader", return_value=mock_reader),
            patch("shutil.which", return_value="/usr/bin/tesseract"),
            patch("pdf2image.convert_from_bytes", return_value=["fake-page-image"]),
            patch("pytesseract.image_to_string", return_value="OCR'd text"),
        ):
            text = extract_pdf_text(self.image)
        self.assertEqual(text, "OCR'd text")

    def test_pdf_reader_exception_falls_through_gracefully(self) -> None:
        with (
            patch("pypdf.PdfReader", side_effect=Exception("corrupt pdf")),
            patch("shutil.which", return_value=None),
        ):
            self.assertIsNone(extract_pdf_text(self.image))


class OcrResourceBoundsTests(TestCase):
    """A PDF's page geometry is attacker-supplied; the raster it becomes must not be.

    `pdf2image` defaults to 200 DPI with no size limit, and a page's dimensions
    come from its own MediaBox - which the spec allows up to 14400pt (200
    inches) a side. A 426-byte PDF declaring that renders to 40,000 x 40,000 px,
    roughly 4.8 GB as RGB, per page, up to `_OCR_MAX_PAGES` deep. Verified
    against the installed poppler: `pdfinfo` reports the declared 14400 x 14400
    pts for exactly such a file, so nothing upstream normalises it.

    Both bounds here are asserted against the *call*, not the render: what
    matters is that a limit is passed to poppler at all, and actually rendering
    the pathological case in a test would spend the memory the bound exists to
    prevent.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("scan.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
        )
        self.no_native_text = MagicMock()
        self.no_native_text.pages = [MagicMock(extract_text=MagicMock(return_value=""))]

    def _run_ocr(self, ocr_result: str = "text"):
        with (
            patch("pypdf.PdfReader", return_value=self.no_native_text),
            patch("shutil.which", return_value="/usr/bin/tesseract"),
            patch("pdf2image.convert_from_bytes", return_value=["page"]) as convert,
            patch("pytesseract.image_to_string", return_value=ocr_result),
        ):
            text = extract_pdf_text(self.image)
        return text, convert

    def test_the_rasteriser_is_given_a_size_bound(self) -> None:
        _, convert = self._run_ocr()

        self.assertIn(
            "size", convert.call_args.kwargs, "poppler was called with no size limit - page geometry decides the raster"
        )
        self.assertEqual(convert.call_args.kwargs["size"], _OCR_MAX_PIXELS)

    def test_the_page_count_bound_is_still_applied(self) -> None:
        """The bound that was already there must not have been displaced."""
        _, convert = self._run_ocr()

        self.assertEqual(convert.call_args.kwargs["last_page"], _OCR_MAX_PAGES)

    def test_the_size_bound_is_a_bare_int_so_poppler_preserves_aspect(self) -> None:
        """A tuple would set the axes independently and could distort or under-bound.

        pdf2image turns an int into poppler's `-scale-to`, which scales the
        longest side - so one number bounds both axes whatever the page shape.
        """
        _, convert = self._run_ocr()

        self.assertIsInstance(convert.call_args.kwargs["size"], int)

    def test_stored_ocr_text_is_capped(self) -> None:
        text, _ = self._run_ocr(ocr_result="x" * (_OCR_MAX_CHARS + 5_000))

        self.assertEqual(len(text), _OCR_MAX_CHARS)

    def test_ordinary_ocr_text_is_returned_whole(self) -> None:
        """Anti-vacuity: the cap must not truncate a real document."""
        text, _ = self._run_ocr(ocr_result="a normal page of scanned text")

        self.assertEqual(text, "a normal page of scanned text")
