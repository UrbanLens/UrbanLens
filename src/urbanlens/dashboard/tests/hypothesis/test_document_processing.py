"""Unit tests for services.documents - LibreOffice/pypdf/OCR-backed document processing.

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
from urbanlens.dashboard.services.documents import CONVERTIBLE_DOCUMENT_EXTENSIONS, convert_to_pdf, extract_pdf_text, soffice_available


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
            image=SimpleUploadedFile("notes.docx", b"doc-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
        with patch("urbanlens.dashboard.services.documents.soffice_available", return_value=False):
            self.assertIsNone(convert_to_pdf(image))

    def test_conversion_failure_returns_none(self) -> None:
        image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("notes.docx", b"doc-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
        with (
            patch("urbanlens.dashboard.services.documents.soffice_available", return_value=True),
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
