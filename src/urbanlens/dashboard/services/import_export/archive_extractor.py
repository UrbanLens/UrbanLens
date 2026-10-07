"""Secure archive extraction and content-type validation for location file imports."""

from __future__ import annotations

from contextlib import contextmanager
import functools
import logging
import os
import re
import struct
import tarfile
import tempfile
from typing import IO, TYPE_CHECKING, NamedTuple
import zipfile

from urbanlens.dashboard.services.import_formats.heuristics import DEFAULT_LATITUDE_KEYS, DEFAULT_LONGITUDE_KEYS, normalize_header_key
from urbanlens.dashboard.services.import_formats.json_stream import top_level_keys
from urbanlens.dashboard.services.import_formats.streams import READ_CHUNK_BYTES, as_stream, is_valid_text, skip_bom_and_whitespace
from urbanlens.dashboard.services.sandbox import untrusted_parse
from urbanlens.dashboard.services.security.redact import redact_filename

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

#: Takes one open archive entry - its base name, its file, and the archive's kind - and returns what to yield for
#: it, or None to skip it.
type _Take[T] = Callable[[str, IO[bytes], str], T | None]

# Archive magic bytes
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"

# Hard limits to prevent resource exhaustion / zip bombs
_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SINGLE_FILE_BYTES = 1 * 1024 * 1024 * 1024
_MAX_FILE_COUNT = 1000
#: The most central directory a ZIP may have. ``zipfile`` reads it whole into a ``ZipInfo`` per entry, about 600 bytes
#: each, before any entry is looked at, so this bounds what opening one costs: about 100 MiB at the most.
MAX_ZIP_DIRECTORY_BYTES = 8 * 1024 * 1024
#: The most members a TAR may have, supported or not; ``tarfile`` keeps each one's ``TarInfo``.
MAX_TAR_MEMBERS = 50_000

# Only files with these extensions are considered when extracting from archives.
# KMZ is included because it is itself a ZIP (containing KML) and may appear inside an outer
# archive. shp/dbf/shx/prj/cpg are Shapefile sidecar parts, which are grouped by filename stem
# elsewhere (see services.import_formats.shapefile) rather than sniffed individually here. html is
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


#: How much of a file's text the format sniff keeps, past any leading whitespace. A first line longer than this is
#: judged on its start.
_SNIFF_CHARS = 64 * 1024


class ExtractedFile(NamedTuple):
    """A single file extracted from an archive."""

    name: str
    data: bytes


class SpooledFile(NamedTuple):
    """A single file extracted from an archive to disk.

    Attributes:
        name: The entry's base name.
        path: Where it was written; the caller removes it.
    """

    name: str
    path: str


class ExtractionBudget:
    """One allowance for everything extracted from a single upload.
    Charging is against bytes actually read rather than the size the archive declares."""

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

    def read_limit(self) -> int:
        """The most one entry may read: the per-file cap, or less when the upload has less left.

        Returns:
            Bytes an entry may read before its size alone decides the outcome.
        """
        return min(_MAX_SINGLE_FILE_BYTES, max(self.remaining_bytes, 0))

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
        data: Raw bytes to inspect, or at least their first four.

    Returns:
        True when the bytes indicate a ZIP or GZIP/TGZ archive."""
    return data[:4] == _ZIP_MAGIC or data[:2] == _GZIP_MAGIC


def is_archive_file(stream: IO[bytes]) -> bool:
    """Whether a file starts with ZIP or GZIP magic bytes, leaving it where it was.

    Args:
        stream: A seekable binary file.

    Returns:
        True when the file is a ZIP or GZIP/TGZ archive.
    """
    start = stream.tell()
    magic = stream.read(len(_ZIP_MAGIC))
    stream.seek(start)
    return is_archive(magic)


def extract_archive(data: bytes, budget: ExtractionBudget | None = None) -> list[ExtractedFile]:
    """Safely extract supported files from a ZIP or TGZ archive.

    Args:
        data: Raw bytes of the archive.
        budget: Allowance to draw on.

    Returns:
        List of :class:`ExtractedFile` for every supported entry found.

    Raises:
        ValueError: If the archive is malformed or exceeds safety limits."""
    return list(iter_archive(data, budget))


def iter_archive(data: bytes | IO[bytes], budget: ExtractionBudget | None = None) -> Iterator[ExtractedFile]:
    """Extract supported files from a ZIP or TGZ archive one at a time, as each is asked for.

    A caller that lets go of an entry before asking for the next holds one entry, not the archive.

    Args:
        data: The archive's bytes, or a seekable binary file positioned at its start.
        budget: Allowance to draw on.

    Returns:
        An iterator of :class:`ExtractedFile`, one per supported entry.

    Raises:
        ValueError: If the archive is not a ZIP or TGZ; while iterating, if it is malformed or exceeds safety limits."""
    budget = budget or ExtractionBudget()
    return _entries(as_stream(data), budget, functools.partial(_read_entry, budget))


def spool_archive(source: IO[bytes], directory: str, budget: ExtractionBudget) -> Iterator[SpooledFile]:
    """Extract supported files from a ZIP or TGZ archive to disk one at a time, as each is asked for.

    An entry is copied a chunk at a time, so the per-entry cap bounds a file on disk rather than one in memory.

    Args:
        source: A seekable binary file positioned at the archive's start.
        directory: Where entries are written. On a tmpfs their pages would count as memory.
        budget: Allowance to draw on.

    Returns:
        An iterator of :class:`SpooledFile`, one per supported entry, each the caller's to remove.

    Raises:
        ValueError: If the archive is not a ZIP or TGZ; while iterating, if it is malformed or exceeds safety limits."""
    return _entries(source, budget, functools.partial(_spool_entry, directory, budget))


def _entries[T](source: IO[bytes], budget: ExtractionBudget, take: _Take[T]) -> Iterator[T]:
    start = source.tell()
    magic = source.read(len(_ZIP_MAGIC))
    source.seek(start)
    if magic == _ZIP_MAGIC:
        return _zip_entries(source, budget, take)
    if magic[:2] == _GZIP_MAGIC:
        return _tgz_entries(source, budget, take)
    raise ValueError("Not a recognized archive format (expected ZIP or GZIP/TGZ).")


def _read_entry(budget: ExtractionBudget, name: str, member: IO[bytes], kind: str) -> ExtractedFile | None:
    # One byte past the limit is enough to tell an entry that exceeds it - its declared size can lie - without
    # holding the rest of it in memory.
    content = member.read(budget.read_limit() + 1)
    budget.claim_bytes(len(content))
    if len(content) > _MAX_SINGLE_FILE_BYTES:
        logger.warning("Actual size of %s entry exceeded declared size, skipping: %s", kind, redact_filename(name))
        return None
    return ExtractedFile(name, content)


def _spool_entry(directory: str, budget: ExtractionBudget, name: str, member: IO[bytes], kind: str) -> SpooledFile | None:
    handle, path = tempfile.mkstemp(prefix="entry-", dir=directory)
    try:
        with os.fdopen(handle, "wb") as out:
            written = _copy(member, out, budget.read_limit() + 1)
        budget.claim_bytes(written)
    except BaseException:
        os.remove(path)
        raise
    if written > _MAX_SINGLE_FILE_BYTES:
        os.remove(path)
        logger.warning("Actual size of %s entry exceeded declared size, skipping: %s", kind, redact_filename(name))
        return None
    return SpooledFile(name, path)


def _copy(source: IO[bytes], out: IO[bytes], limit: int) -> int:
    written = 0
    while written < limit and (chunk := source.read(min(READ_CHUNK_BYTES, limit - written))):
        out.write(chunk)
        written += len(chunk)
    return written


def validate_content_type(name: str, data: bytes | IO[bytes]) -> str | None:
    """Validate file content and return its format string, or ``None`` if unsupported.

    The whole file is read, a chunk at a time, but only its start is kept: the format is judged on that, while
    the whole must be UTF-8 (unless it is binary WKB) and, when it looks like JSON, one well-formed JSON value.

    Args:
        name: Filename used only for diagnostic logging.
        data: The file's bytes, or a seekable binary file, which is left where it was.

    Returns:
        Format string, or ``None`` when the content is unrecognised or invalid."""
    stream = as_stream(data)
    start = stream.tell()
    try:
        return _sniff(name, stream)
    finally:
        stream.seek(start)


def _sniff(name: str, stream: IO[bytes]) -> str | None:
    start = stream.tell()
    magic = stream.read(16)
    if len(magic) < 4:
        logger.debug("Skipping file too small to validate: %s", redact_filename(name))
        return None

    # Binary WKB is checked before the UTF-8 decode attempt below, since it is
    # (by definition) not text.
    if _sniff_wkb(magic):
        return "wkb"

    # Binary files (those that can't decode as UTF-8) are rejected outright. utf-8-sig strips a
    # leading BOM so Excel "CSV UTF-8" exports whose first header is ``latitude`` still sniff as CSV
    # (plain utf-8 leaves the BOM glued to that header, and str.lstrip() does not remove \\ufeff).
    stream.seek(start)
    if not is_valid_text(stream, "utf-8-sig"):
        logger.debug("Skipping non-UTF-8 file: %s", redact_filename(name))
        return None
    stream.seek(start)
    text = skip_bom_and_whitespace(stream, limit=_SNIFF_CHARS)

    if not text:
        return None

    # JSON: must start with '{' or '[' and parse successfully.
    # Recognised variants: "json" - GeoJSON (Takeout "Saved Places" or generic FeatureCollection)
    if text[0] in "{[":
        stream.seek(start)
        keys = top_level_keys(stream)
        if keys is None:
            logger.debug("File is not one well-formed JSON object: %s", redact_filename(name))
            return None
        if "features" in keys:
            return "json"
        if "timelineObjects" in keys:
            return "location_history"
        logger.debug("File is valid JSON but not a recognised import format: %s", redact_filename(name))
        return None

    # XML: dispatch on root tag (KML/GPX/OSM XML all share the same shape otherwise).
    if text.startswith(("<?xml", "<kml", "<gpx", "<osm")):
        window = text[:2000]
        for tag, fmt in _XML_TAG_FORMATS:
            if tag in window:
                return fmt
        logger.debug("File is XML but does not match a known format: %s", redact_filename(name))
        return None

    # HTML: Google Takeout's My Activity export.
    # Checked before the WKT/CSV heuristics below - a huge single-line My Activity file's <title>
    # tag or "mdl-typography--title" class name would otherwise trip the CSV header heuristic's
    # "title" substring check and get misclassified as CSV.
    if text[:20].lower().startswith(("<!doctype html", "<html")):
        from urbanlens.dashboard.services.apis.locations.google.my_activity import looks_like_my_activity

        if looks_like_my_activity(text[:4000]):
            return "my_activity"
        logger.debug("File is HTML but not a recognised My Activity export: %s", redact_filename(name))
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

    logger.debug("File content did not match any supported import format: %s", redact_filename(name))
    return None


_WKB_GEOMETRY_TYPE_CODES = frozenset(range(1, 8))  # Point .. GeometryCollection


def _sniff_wkb(data: bytes) -> bool:
    """Return True if *data* looks like a binary WKB geometry."""
    if len(data) < 5 or data[0] not in (0, 1):
        return False
    endianness = "<" if data[0] == 1 else ">"
    try:
        (geom_code,) = struct.unpack_from(f"{endianness}I", data, 1)
    except struct.error:
        return False
    return (geom_code & 0xFFFF) % 1000 in _WKB_GEOMETRY_TYPE_CODES


class ZipDirectoryTooLargeError(zipfile.BadZipFile):
    """A ZIP's central directory is over :data:`MAX_ZIP_DIRECTORY_BYTES`."""


class _DirectoryReadBound:
    """A file whose reads are refused past a size while ``zipfile`` opens it.

    ``zipfile`` reads the central directory in one read of the size the end record gives, and parses entries until
    that many bytes are used, whatever count the record claims. Refusing that read refuses the directory before any
    of it is held. Every other read it makes while opening is a few bytes, or the end record's last 64 KiB.

    Each read asks the file for no more than it holds past its position: a buffered ``read(n)`` allocates ``n`` bytes
    before it reads, so answering ``zipfile``'s read-to-end of the end record with the limit would allocate all of it
    for every ZIP opened (see UrbanLens#298 ("Nine tests fail under bin/host_pytest.sh on release/v_0_9_0")).
    """

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        self._raw = raw
        self.limit: int | None = limit

    def read(self, size: int | None = -1, /) -> bytes:
        if self.limit is None:
            return self._raw.read(-1 if size is None else size)
        if size is not None and size > self.limit:
            raise ZipDirectoryTooLargeError(f"the central directory is {size} bytes, over {self.limit}")
        remaining = self._remaining()
        if size is None or size < 0:
            if remaining > self.limit:
                raise ZipDirectoryTooLargeError(f"a read while opening the archive was over {self.limit} bytes")
            size = remaining
        return self._raw.read(min(size, remaining))

    def _remaining(self) -> int:
        """How many bytes the file holds past its position, which is left where it was."""
        position = self._raw.tell()
        end = self._raw.seek(0, os.SEEK_END)
        self._raw.seek(position)
        return max(end - position, 0)

    def seek(self, offset: int, whence: int = os.SEEK_SET, /) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def seekable(self) -> bool:
        return self._raw.seekable()

    def flush(self) -> None:
        self._raw.flush()


@contextmanager
def open_zip(source: IO[bytes]) -> Iterator[zipfile.ZipFile]:
    """``zipfile.ZipFile`` over *source*, refusing a central directory over :data:`MAX_ZIP_DIRECTORY_BYTES` unread.

    Args:
        source: A seekable binary file holding the archive.

    Yields:
        The open archive.

    Raises:
        ZipDirectoryTooLargeError: The directory is over the bound.
        zipfile.BadZipFile: The archive is not a ZIP.
    """
    bounded = _DirectoryReadBound(source, MAX_ZIP_DIRECTORY_BYTES)
    with zipfile.ZipFile(bounded) as archive:
        bounded.limit = None
        yield archive


# Internal helpers


def _safe_basename(path: str) -> str | None:
    """Return the basename of *path*, or ``None`` if any component is suspicious."""
    parts = path.replace("\\", "/").split("/")
    basename = parts[-1]
    if ".." in parts or not basename:
        return None
    return basename


def _extension(filename: str) -> str:
    """Return the lowercase extension of *filename* without the leading dot."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


@untrusted_parse("archive.extract")
def _zip_entries[T](source: IO[bytes], budget: ExtractionBudget, take: _Take[T]) -> Iterator[T]:
    """Hand each supported ZIP entry to *take*, one at a time, drawing on *budget*."""
    try:
        with open_zip(source) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                # Skip symlinks: check Unix mode bits stored in external_attr.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    logger.warning("Skipping symlink in ZIP: %s", redact_filename(info.filename))
                    continue

                safe_name = _safe_basename(info.filename)
                if not safe_name:
                    logger.warning("Skipping unsafe path in ZIP: %s", redact_filename(info.filename))
                    continue

                if _extension(safe_name) not in _ARCHIVE_ALLOWED_EXTENSIONS:
                    continue

                if info.file_size > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Skipping oversized entry in ZIP: %s (%d bytes)",
                        redact_filename(safe_name),
                        info.file_size,
                    )
                    continue

                budget.claim_file()

                with zf.open(info) as member:
                    taken = take(safe_name, member, "ZIP")
                if taken is not None:
                    yield taken

    except ZipDirectoryTooLargeError as exc:
        logger.info("ZIP archive refused: %s", exc)
        raise ValueError("The ZIP archive lists too many files.") from exc
    except zipfile.BadZipFile as exc:
        # zipfile's messages can quote a member's file name ("File name in directory ... and header ... differ").
        logger.info("Invalid ZIP archive: %s", type(exc).__name__)
        raise ValueError("Invalid ZIP archive.") from exc


@untrusted_parse("archive.extract")
def _tgz_entries[T](source: IO[bytes], budget: ExtractionBudget, take: _Take[T]) -> Iterator[T]:
    """Hand each supported member of a GZIP-compressed TAR archive to *take*, one at a time, drawing on *budget*."""
    try:
        with tarfile.open(fileobj=source, mode="r:gz") as tf:
            for count, member in enumerate(tf, start=1):
                if count > MAX_TAR_MEMBERS:
                    raise ValueError("The TGZ archive holds too many files.")
                # Only regular files - skip dirs, symlinks, hardlinks, devices.
                if not member.isfile():
                    continue

                safe_name = _safe_basename(member.name)
                if not safe_name:
                    logger.warning("Skipping unsafe path in TGZ: %s", redact_filename(member.name))
                    continue

                if _extension(safe_name) not in _ARCHIVE_ALLOWED_EXTENSIONS:
                    continue

                if member.size > _MAX_SINGLE_FILE_BYTES:
                    logger.warning(
                        "Skipping oversized member in TGZ: %s (%d bytes)",
                        redact_filename(safe_name),
                        member.size,
                    )
                    continue

                budget.claim_file()

                fobj = tf.extractfile(member)
                if fobj is None:
                    continue

                with fobj:
                    taken = take(safe_name, fobj, "TGZ")
                if taken is not None:
                    yield taken

    except tarfile.TarError as exc:
        logger.info("Invalid TGZ archive: %s", exc)
        raise ValueError("Invalid TGZ archive.") from exc
