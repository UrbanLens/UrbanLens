"""Secure archive extraction and content-type validation for location file imports."""

from __future__ import annotations

import io
import json
import logging
import tarfile
from typing import NamedTuple
import zipfile

logger = logging.getLogger(__name__)

# Archive magic bytes
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"

# Hard limits to prevent resource exhaustion / zip bombs
_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total across all extracted files
_MAX_SINGLE_FILE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB per individual file
_MAX_FILE_COUNT = 1000

# Only files with these extensions are considered when extracting from archives.
# KMZ is included because it is itself a ZIP (containing KML) and may appear inside
# an outer archive.
_ARCHIVE_ALLOWED_EXTENSIONS = frozenset({"json", "kml", "csv", "kmz"})


class ExtractedFile(NamedTuple):
    """A single file extracted from an archive."""

    name: str
    data: bytes


def is_archive(data: bytes) -> bool:
    """Returns True if *data* starts with ZIP or GZIP magic bytes.

    Args:
        data: Raw bytes to inspect (at least 4 bytes recommended).

    Returns:
        True when the bytes indicate a ZIP or GZIP/TGZ archive.
    """
    return data[:4] == _ZIP_MAGIC or data[:2] == _GZIP_MAGIC


def extract_archive(data: bytes) -> list[ExtractedFile]:
    """Safely extract supported files from a ZIP or TGZ archive.

    Security measures applied:
    - Type verified by magic bytes, not filename extension.
    - Path-traversal entries (``../`` or absolute paths) are silently skipped.
    - Symlinks and non-regular-file entries are skipped.
    - Per-file and cumulative uncompressed-size limits enforced.
    - Only entries whose extension is in ``_ARCHIVE_ALLOWED_EXTENSIONS`` are extracted.

    Args:
        data: Raw bytes of the archive.

    Returns:
        List of :class:`ExtractedFile` for every supported entry found.

    Raises:
        ValueError: If the archive is malformed or exceeds safety limits.
    """
    if data[:4] == _ZIP_MAGIC:
        return _extract_zip(data)
    if data[:2] == _GZIP_MAGIC:
        return _extract_tgz(data)
    raise ValueError("Not a recognized archive format (expected ZIP or GZIP/TGZ).")


def validate_content_type(name: str, data: bytes) -> str | None:
    """Validate file content and return its format string, or ``None`` if unsupported.

    Validation is performed on the *content* of the file, not just its extension,
    to guard against misnamed or deliberately misleading uploads.

    Supported return values: ``'json'``, ``'kml'``, ``'csv'``.
    KMZ files are handled at the archive-extraction layer and are not returned here.

    Args:
        name: Filename used only for diagnostic logging.
        data: Raw file bytes.

    Returns:
        Format string, or ``None`` when the content is unrecognised or invalid.
    """
    if len(data) < 4:
        logger.debug("Skipping file too small to validate: %s", name)
        return None

    # Binary files (those that can't decode as UTF-8) are rejected outright.
    try:
        text = data.decode("utf-8").lstrip()
    except UnicodeDecodeError:
        logger.debug("Skipping non-UTF-8 file: %s", name)
        return None

    if not text:
        return None

    # JSON: must start with '{' or '[' and parse successfully.
    # Recognised variants:
    #   "json"             - GeoJSON Saved Places (has "features")
    #   "location_history" - Google Semantic Location History (has "timelineObjects")
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("File has JSON-like start but failed to parse: %s", name)
            return None
        if isinstance(parsed, dict):
            if "features" in parsed:
                return "json"
            if "timelineObjects" in parsed:
                return "location_history"
        logger.debug("File is valid JSON but not a recognised import format: %s", name)
        return None

    # KML: XML document that contains a <kml element within the first 2 kB.
    if text.startswith(("<?xml", "<kml")):
        if "<kml" in text[:2000]:
            return "kml"
        logger.debug("File is XML but does not look like KML: %s", name)
        return None

    # CSV: must be parseable text with at least one expected Google Takeout header.
    first_line = text.split("\n", 1)[0].lower()
    if any(h in first_line for h in ("url", "title", "note")):
        return "csv"

    logger.debug("File content did not match any supported import format: %s", name)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_basename(path: str) -> str | None:
    """Return the basename of *path*, or ``None`` if any component is suspicious.

    Rejects paths that contain ``..`` components or that are absolute
    (start with ``/`` on Unix or a drive letter on Windows).
    """
    parts = path.replace("\\", "/").split("/")
    basename = parts[-1]
    if ".." in parts or not basename:
        return None
    return basename


def _extension(filename: str) -> str:
    """Return the lowercase extension of *filename* without the leading dot."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _extract_zip(data: bytes) -> list[ExtractedFile]:
    """Extract supported files from a ZIP archive."""
    results: list[ExtractedFile] = []
    total_size = 0
    file_count = 0

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                # Skip symlinks: check Unix mode bits stored in external_attr.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    logger.warning("Skipping symlink in ZIP: %s", info.filename)
                    continue

                safe_name = _safe_basename(info.filename)
                if not safe_name:
                    logger.warning("Skipping unsafe path in ZIP: %s", info.filename)
                    continue

                if _extension(safe_name) not in _ARCHIVE_ALLOWED_EXTENSIONS:
                    continue

                if info.file_size > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Skipping oversized entry in ZIP: %s (%d bytes)",
                        safe_name,
                        info.file_size,
                    )
                    continue

                total_size += info.file_size
                if total_size > _MAX_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"Archive exceeds {_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB uncompressed.",
                    )

                file_count += 1
                if file_count > _MAX_FILE_COUNT:
                    raise ValueError(
                        f"Archive contains more than {_MAX_FILE_COUNT} supported files.",
                    )

                with zf.open(info) as f:
                    # Read one extra byte so we can detect if the actual size
                    # exceeds the declared file_size (compression-ratio attack).
                    content = f.read(_MAX_SINGLE_FILE_BYTES + 1)

                if len(content) > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Actual size of ZIP entry exceeded declared size, skipping: %s",
                        safe_name,
                    )
                    continue

                results.append(ExtractedFile(safe_name, content))

    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP archive: {exc}") from exc

    return results


def _extract_tgz(data: bytes) -> list[ExtractedFile]:
    """Extract supported files from a GZIP-compressed TAR archive."""
    results: list[ExtractedFile] = []
    total_size = 0
    file_count = 0

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                # Only regular files - skip dirs, symlinks, hardlinks, devices.
                if not member.isfile():
                    continue

                safe_name = _safe_basename(member.name)
                if not safe_name:
                    logger.warning("Skipping unsafe path in TGZ: %s", member.name)
                    continue

                if _extension(safe_name) not in _ARCHIVE_ALLOWED_EXTENSIONS:
                    continue

                if member.size > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Skipping oversized member in TGZ: %s (%d bytes)",
                        safe_name,
                        member.size,
                    )
                    continue

                total_size += member.size
                if total_size > _MAX_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"Archive exceeds {_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB uncompressed.",
                    )

                file_count += 1
                if file_count > _MAX_FILE_COUNT:
                    raise ValueError(
                        f"Archive contains more than {_MAX_FILE_COUNT} supported files.",
                    )

                fobj = tf.extractfile(member)
                if fobj is None:
                    continue

                content = fobj.read(_MAX_SINGLE_FILE_BYTES + 1)
                if len(content) > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Actual size of TGZ member exceeded declared size, skipping: %s",
                        safe_name,
                    )
                    continue

                results.append(ExtractedFile(safe_name, content))

    except tarfile.TarError as exc:
        raise ValueError(f"Invalid TGZ archive: {exc}") from exc

    return results
