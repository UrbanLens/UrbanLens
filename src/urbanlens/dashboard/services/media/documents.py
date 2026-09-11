"""Document processing utilities - convert uploads to PDF and make them searchable.
Every function here degrades gracefully (logs and returns None/unchanged) when a binary is missing, rather than failing the upload."""

from __future__ import annotations

import logging
import posixpath
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.media.images import StoredFileReplacement
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

_SOFFICE_TIMEOUT_SECONDS = 120
_OCR_MAX_PAGES = 25

#: Longest edge, in pixels, any page may be rasterised to for OCR.
#: Passed as a bare int, which pdf2image turns into poppler's `-scale-to` - longest side, aspect
#: preserved - so one number bounds both axes whatever the page shape.
_OCR_MAX_PIXELS = 2200

#: Ceiling on stored OCR text.
#: Truncated rather than discarded - partial text still serves the search it was extracted for.
_OCR_MAX_CHARS = 200_000

# Extensions LibreOffice can convert to PDF. Anything else that isn't already
# a PDF is stored as-is (no conversion attempted).
CONVERTIBLE_DOCUMENT_EXTENSIONS = frozenset({".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"})
DOCUMENT_EXTENSIONS = CONVERTIBLE_DOCUMENT_EXTENSIONS | {".pdf"}


def soffice_path() -> str | None:
    """The absolute path to the LibreOffice headless binary, or None.

    Resolved once here rather than left to `exec`'s own PATH walk - see the
    note on `ffmpeg_path` in `videos.py` for what that does and does not buy.
    """
    return shutil.which("soffice")


def soffice_available() -> bool:
    """Whether the LibreOffice headless binary is present on PATH."""
    return soffice_path() is not None


@untrusted_parse("document.convert")
def convert_to_pdf(image: Image) -> StoredFileReplacement | None:
    """Convert a non-PDF document upload to PDF in place, via LibreOffice headless.
    The stored file is replaced via the storage abstraction so this works regardless of storage backend.

    Args:
        image: The Image row whose stored document to convert.

    Returns:
        The replacement when the file was converted, else None. Its
        ``superseded_name`` is still on disk; the caller deletes it once the row
        names the PDF - see
        :func:`~urbanlens.dashboard.services.media.images.discard_superseded_file`."""
    old_name = image.image.name
    if not old_name:
        return None
    ext = posixpath.splitext(old_name)[1].lower()
    if ext == ".pdf":
        return None
    soffice = soffice_path()
    if ext not in CONVERTIBLE_DOCUMENT_EXTENSIONS or soffice is None:
        return None

    old_size = image.image.size
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = posixpath.join(tmpdir, "source" + ext)
        with image.image.open("rb") as stored_file, open(src_path, "wb") as src_file:
            shutil.copyfileobj(stored_file, src_file)

        try:
            subprocess.run(
                [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, src_path],
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
    logger.info("Converted document %s to PDF: %s -> %s bytes", image.pk, old_size, len(new_bytes))
    return StoredFileReplacement(len(new_bytes), old_name if image.image.name != old_name else None)


@untrusted_parse("document.ocr")
def extract_pdf_text(image: Image) -> str | None:
    """Extract searchable text from a stored PDF: its native text layer plus OCR.
    Both are best-effort - missing binaries or unparseable PDFs simply contribute no text rather than failing the upload.

    Args:
        image: The Image row whose stored PDF to extract text from.

    Returns:
        The combined text, or None if nothing could be extracted."""
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
            pages = convert_from_bytes(pdf_bytes, last_page=_OCR_MAX_PAGES, size=_OCR_MAX_PIXELS)
            for page_image in pages:
                if text := pytesseract.image_to_string(page_image).strip():
                    chunks.append(text)
        except Exception:
            logger.warning("OCR fallback failed for image %s", image.pk, exc_info=True)

    if not chunks:
        return None
    combined = "\n\n".join(chunks)
    if len(combined) > _OCR_MAX_CHARS:
        logger.warning("OCR text for image %s truncated from %d to %d characters", image.pk, len(combined), _OCR_MAX_CHARS)
        combined = combined[:_OCR_MAX_CHARS]
    return combined
