"""Secure archive extraction and content-type validation for location file imports."""

from __future__ import annotations

import io
import json
import logging
import re
import struct
import tarfile
from typing import NamedTuple
import zipfile

from urbanlens.dashboard.services.import_formats.heuristics import DEFAULT_LATITUDE_KEYS, DEFAULT_LONGITUDE_KEYS, normalize_header_key
from urbanlens.dashboard.services.sandbox import untrusted_parse

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
# an outer archive. shp/dbf/shx/prj/cpg are Shapefile sidecar parts, which are
# grouped by filename stem elsewhere (see services.import_formats.shapefile)
# rather than sniffed individually here. html is for Google Takeout's My Activity
# export, which ships as Takeout/My Activity/Maps/MyActivity.html inside a zipped
# Takeout archive.
_ARCHIVE_ALLOWED_EXTENSIONS = frozenset(
    {"json", "kml", "csv", "kmz", "gpx", "geojson", "wkt", "wkb", "osm", "shp", "dbf", "shx", "prj", "cpg", "html"},
)

# XML root tags recognised at the archive-extraction/import-format-sniffing layer,
# mapped to their format string. Checked in order; the first match within the
# sniff window wins.
_XML_TAG_FORMATS: tuple[tuple[str, str], ...] = (
    ("<kml", "kml"),
    ("<gpx", "gpx"),
    ("<osm", "osm_xml"),
)

# WKT geometry type keywords (case-insensitive), optionally followed by a Z/M/ZM
# dimensionality suffix (e.g. "POINT Z", "LINESTRING ZM").
_WKT_GEOMETRY_RE = re.compile(
    r"^(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)\s*(Z|M|ZM)?\s*\(",
    re.IGNORECASE,
)


class ExtractedFile(NamedTuple):
    """A single file extracted from an archive."""

    name: str
    data: bytes


class ExtractionBudget:
    """One allowance for everything extracted from a single upload.

    The limits above are per *archive*, which is not the same as per upload:
    an upload can contain several archives, and an archive can contain nested
    archives that callers expand with a second `extract_archive` call. Each of
    those calls used to start a fresh 2 GB / 1000-file allowance, so an outer
    ZIP holding N nested bombs cost N x 2 GB - the cap bound each call and
    nothing bound the total. Callers that expand nested archives must create
    one of these and pass it to every call.

    Charging is against bytes actually read rather than the size the archive
    declares. That is exactness, not a hole being closed: CPython's ``zipfile``
    bounds a read by the declared ``file_size`` and then fails the CRC check,
    so a understated declaration truncates rather than overruns (verified
    against 3.12 - patching a 5 MB entry's declared size to 1 raises
    ``BadZipFile: Bad CRC-32``, it does not hand back 5 MB). The declared value
    was therefore always a valid upper bound; the actual length is simply the
    right thing to bill.
    """

    def __init__(self, max_bytes: int = _MAX_UNCOMPRESSED_BYTES, max_files: int = _MAX_FILE_COUNT) -> None:
        """Start an allowance.

        Args:
            max_bytes: Total uncompressed bytes permitted across the upload.
            max_files: Total supported entries permitted across the upload.
        """
        self.remaining_bytes = max_bytes
        self.remaining_files = max_files

    def claim_file(self) -> None:
        """Account for one extracted entry.

        Raises:
            ValueError: The upload holds more supported files than permitted.
        """
        self.remaining_files -= 1
        if self.remaining_files < 0:
            raise ValueError(f"Archive contains more than {_MAX_FILE_COUNT} supported files.")

    def claim_bytes(self, size: int) -> None:
        """Account for *size* uncompressed bytes.

        Args:
            size: Bytes to deduct from the remaining allowance.

        Raises:
            ValueError: The upload expands past the permitted total.
        """
        self.remaining_bytes -= size
        if self.remaining_bytes < 0:
            raise ValueError(f"Archive exceeds {_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB uncompressed.")


def is_archive(data: bytes) -> bool:
    """Returns True if *data* starts with ZIP or GZIP magic bytes.

    Args:
        data: Raw bytes to inspect (at least 4 bytes recommended).

    Returns:
        True when the bytes indicate a ZIP or GZIP/TGZ archive.
    """
    return data[:4] == _ZIP_MAGIC or data[:2] == _GZIP_MAGIC


def extract_archive(data: bytes, budget: ExtractionBudget | None = None) -> list[ExtractedFile]:
    """Safely extract supported files from a ZIP or TGZ archive.

    Security measures applied:
    - Type verified by magic bytes, not filename extension.
    - Path-traversal entries (``../`` or absolute paths) are silently skipped.
    - Symlinks and non-regular-file entries are skipped.
    - Per-file and cumulative uncompressed-size limits enforced, the cumulative
      ones shared across every call that passes the same ``budget``.
    - Only entries whose extension is in ``_ARCHIVE_ALLOWED_EXTENSIONS`` are extracted.

    Args:
        data: Raw bytes of the archive.
        budget: Allowance to draw on. Callers expanding *nested* archives must
            create one :class:`ExtractionBudget` and pass it to every call, so
            the whole upload shares one limit; omitting it gives this archive
            its own, which is only correct when nothing else will be extracted.

    Returns:
        List of :class:`ExtractedFile` for every supported entry found.

    Raises:
        ValueError: If the archive is malformed or exceeds safety limits.
    """
    if budget is None:
        budget = ExtractionBudget()
    if data[:4] == _ZIP_MAGIC:
        return _extract_zip(data, budget)
    if data[:2] == _GZIP_MAGIC:
        return _extract_tgz(data, budget)
    raise ValueError("Not a recognized archive format (expected ZIP or GZIP/TGZ).")


def validate_content_type(name: str, data: bytes) -> str | None:
    """Validate file content and return its format string, or ``None`` if unsupported.

    Validation is performed on the *content* of the file, not just its extension,
    to guard against misnamed or deliberately misleading uploads.

    Supported return values: ``'json'``, ``'kml'``, ``'csv'``, ``'location_history'``,
    ``'gpx'``, ``'wkt'``, ``'wkb'``, ``'osm_xml'``, ``'my_activity'``. KMZ files are
    handled at the archive-extraction layer and are not returned here. Shapefile parts
    (``.shp``/``.dbf``/``.shx``/``.prj``/``.cpg``) are not sniffed here either -
    they are grouped by filename stem in ``services.import_formats.shapefile``
    before this function is ever consulted for them.

    Args:
        name: Filename used only for diagnostic logging.
        data: Raw file bytes.

    Returns:
        Format string, or ``None`` when the content is unrecognised or invalid.
    """
    if len(data) < 4:
        logger.debug("Skipping file too small to validate: %s", name)
        return None

    # Binary WKB is checked before the UTF-8 decode attempt below, since it is
    # (by definition) not text.
    if _sniff_wkb(data):
        return "wkb"

    # Binary files (those that can't decode as UTF-8) are rejected outright.
    # utf-8-sig strips a leading BOM so Excel "CSV UTF-8" exports whose first
    # header is ``latitude`` still sniff as CSV (plain utf-8 leaves the BOM
    # glued to that header, and str.lstrip() does not remove \\ufeff).
    try:
        text = data.decode("utf-8-sig").lstrip()
    except UnicodeDecodeError:
        logger.debug("Skipping non-UTF-8 file: %s", name)
        return None

    if not text:
        return None

    # JSON: must start with '{' or '[' and parse successfully.
    # Recognised variants:
    #   "json"             - GeoJSON (Takeout "Saved Places" or generic FeatureCollection)
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

    # XML: dispatch on root tag (KML/GPX/OSM XML all share the same shape otherwise).
    if text.startswith(("<?xml", "<kml", "<gpx", "<osm")):
        window = text[:2000]
        for tag, fmt in _XML_TAG_FORMATS:
            if tag in window:
                return fmt
        logger.debug("File is XML but does not match a known format: %s", name)
        return None

    # HTML: Google Takeout's My Activity export. Checked before the WKT/CSV
    # heuristics below - a huge single-line My Activity file's <title> tag or
    # "mdl-typography--title" class name would otherwise trip the CSV header
    # heuristic's "title" substring check and get misclassified as CSV.
    if text[:20].lower().startswith(("<!doctype html", "<html")):
        from urbanlens.dashboard.services.apis.locations.google.my_activity import looks_like_my_activity

        if looks_like_my_activity(text[:4000]):
            return "my_activity"
        logger.debug("File is HTML but not a recognised My Activity export: %s", name)
        return None

    # WKT: first token is a recognised geometry keyword, e.g. "POINT (...)".
    if _WKT_GEOMETRY_RE.match(text):
        return "wkt"

    first_line = text.split("\n", 1)[0].strip()

    # Hex-encoded WKB text (e.g. copy-pasted from a PostGIS client's
    # ST_AsHexEWKB output): same geometry-type sniff as binary WKB, applied to
    # the decoded bytes of the first line.
    if len(first_line) >= 10 and len(first_line) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in first_line):
        try:
            if _sniff_wkb(bytes.fromhex(first_line)):
                return "wkb"
        except ValueError:
            pass

    # CSV: either a Google Takeout export (recognised by its header keywords) or a
    # generic spreadsheet export that has its own explicit latitude and longitude
    # columns (e.g. from Airtable, Google Sheets, or Excel).
    if any(h in first_line.lower() for h in ("url", "title", "note")):
        return "csv"

    columns = {normalize_header_key(column.strip('"')) for column in first_line.split(",")}
    has_latitude_column = any(key in columns for key in DEFAULT_LATITUDE_KEYS)
    has_longitude_column = any(key in columns for key in DEFAULT_LONGITUDE_KEYS)
    if has_latitude_column and has_longitude_column:
        return "csv"

    logger.debug("File content did not match any supported import format: %s", name)
    return None


_WKB_GEOMETRY_TYPE_CODES = frozenset(range(1, 8))  # Point .. GeometryCollection


def _sniff_wkb(data: bytes) -> bool:
    """Return True if *data* looks like a binary WKB geometry.

    Checks the leading byte-order flag (``0x00``/``0x01``) and the following
    4-byte geometry-type code, masking out PostGIS EWKB's SRID/Z/M flag bits
    and ISO SQL/MM's ``+1000``/``+2000``/``+3000`` dimensionality offsets so
    every common WKB dialect is recognised.
    """
    if len(data) < 5 or data[0] not in (0, 1):
        return False
    endianness = "<" if data[0] == 1 else ">"
    try:
        (geom_code,) = struct.unpack_from(f"{endianness}I", data, 1)
    except struct.error:
        return False
    return (geom_code & 0xFFFF) % 1000 in _WKB_GEOMETRY_TYPE_CODES


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


@untrusted_parse("archive.extract")
def _extract_zip(data: bytes, budget: ExtractionBudget) -> list[ExtractedFile]:
    """Extract supported files from a ZIP archive, drawing on *budget*."""
    results: list[ExtractedFile] = []

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

                budget.claim_file()

                with zf.open(info) as f:
                    # Read one extra byte so we can detect if the actual size
                    # exceeds the declared file_size (compression-ratio attack).
                    content = f.read(_MAX_SINGLE_FILE_BYTES + 1)

                budget.claim_bytes(len(content))

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


@untrusted_parse("archive.extract")
def _extract_tgz(data: bytes, budget: ExtractionBudget) -> list[ExtractedFile]:
    """Extract supported files from a GZIP-compressed TAR archive, drawing on *budget*."""
    results: list[ExtractedFile] = []

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

                budget.claim_file()

                fobj = tf.extractfile(member)
                if fobj is None:
                    continue

                content = fobj.read(_MAX_SINGLE_FILE_BYTES + 1)
                budget.claim_bytes(len(content))
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
