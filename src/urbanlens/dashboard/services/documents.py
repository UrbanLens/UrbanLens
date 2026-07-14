"""Document processing utilities - convert uploads to PDF and make them searchable.

Requires ``soffice`` (LibreOffice headless, for non-PDF conversion),
``pdftoppm``/``pdftocairo`` (via the ``pdf2image`` package, for OCR fallback),
and ``tesseract`` (via ``pytesseract``) on PATH - see the Dockerfile. Every
function here degrades gracefully (logs and returns None/unchanged) when a
binary is missing, rather than failing the upload.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

_SOFFICE_TIMEOUT_SECONDS = 120
_OCR_MAX_PAGES = 25

# Extensions LibreOffice can convert to PDF. Anything else that isn't already
# a PDF is stored as-is (no conversion attempted).
CONVERTIBLE_DOCUMENT_EXTENSIONS = frozenset({".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"})
DOCUMENT_EXTENSIONS = CONVERTIBLE_DOCUMENT_EXTENSIONS | {".pdf"}


def soffice_available() -> bool:
    """Whether the LibreOffice headless binary is present on PATH."""
    return shutil.which("soffice") is not None


def convert_to_pdf(image: Image) -> int | None:
    """Convert a non-PDF document upload to PDF in place, via LibreOffice headless.

    A file that's already a PDF is left untouched (returns None). The stored
    file is replaced via the storage abstraction so this works regardless of
    storage backend.

    Args:
        image: The Image row whose stored document to convert.

    Returns:
        The new stored size in bytes when converted, else None.
    """
    old_name = image.image.name
    if not old_name:
        return None
    ext = posixpath.splitext(old_name)[1].lower()
    if ext == ".pdf":
        return None
    if ext not in CONVERTIBLE_DOCUMENT_EXTENSIONS or not soffice_available():
        return None

    old_size = image.image.size
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = posixpath.join(tmpdir, "source" + ext)
        with image.image.open("rb") as stored_file, open(src_path, "wb") as src_file:
            shutil.copyfileobj(stored_file, src_file)

        try:
            subprocess.run(
                ["soffice", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, src_path],
                capture_output=True,
                timeout=_SOFFICE_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Document-to-PDF conversion failed for image %s: %s", image.pk, exc, exc_info=True)
            return None

        out_path = posixpath.join(tmpdir, "source.pdf")
        try:
            with open(out_path, "rb") as f:
                new_bytes = f.read()
        except OSError:
            logger.warning("Document-to-PDF conversion produced no output for image %s", image.pk)
            return None

    if not new_bytes:
        return None

    from django.core.files.base import ContentFile

    stem = posixpath.splitext(posixpath.basename(old_name))[0]
    image.image.save(f"{stem}.pdf", ContentFile(new_bytes), save=False)
    if image.image.name != old_name:
        with contextlib.suppress(OSError):
            image.image.storage.delete(old_name)
    logger.info("Converted document %s to PDF: %s -> %s bytes", image.pk, old_size, len(new_bytes))
    return len(new_bytes)


def extract_pdf_text(image: Image) -> str | None:
    """Extract searchable text from a stored PDF: its native text layer plus OCR.

    The native text layer (via ``pypdf``) is cheap and accurate for born-
    digital PDFs; OCR (via ``pdf2image`` + ``pytesseract``) additionally
    covers scanned pages or embedded images that have no text layer. Both are
    best-effort - missing binaries or unparseable PDFs simply contribute no
    text rather than failing the upload.

    Args:
        image: The Image row whose stored PDF to extract text from.

    Returns:
        The combined text, or None if nothing could be extracted.
    """
    if not image.image.name or posixpath.splitext(image.image.name)[1].lower() != ".pdf":
        return None

    chunks: list[str] = []

    try:
        from pypdf import PdfReader

        with image.image.open("rb") as stored_file:
            reader = PdfReader(stored_file)
            for page in reader.pages[:_OCR_MAX_PAGES]:
                if text := (page.extract_text() or "").strip():
                    chunks.append(text)
    except Exception:
        logger.warning("Native PDF text extraction failed for image %s", image.pk, exc_info=True)

    # OCR fallback only for pages that yielded no native text - a born-digital
    # PDF with a full text layer doesn't need it, and OCR-ing every page of a
    # long document would be slow for no benefit.
    if not chunks and shutil.which("tesseract"):
        try:
            from pdf2image import convert_from_bytes
            import pytesseract

            with image.image.open("rb") as stored_file:
                pdf_bytes = stored_file.read()
            pages = convert_from_bytes(pdf_bytes, last_page=_OCR_MAX_PAGES)
            for page_image in pages:
                if text := pytesseract.image_to_string(page_image).strip():
                    chunks.append(text)
        except Exception:
            logger.warning("OCR fallback failed for image %s", image.pk, exc_info=True)

    return "\n\n".join(chunks) if chunks else None
