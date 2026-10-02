"""WKT/WKB pin import."""

from __future__ import annotations

import logging
from typing import IO, TYPE_CHECKING, Any

import shapely.errors
import shapely.wkb
import shapely.wkt

from urbanlens.dashboard.services.import_formats.streams import as_stream, is_valid_text, iter_decoded, iter_lines
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Iterator

    from shapely.geometry.base import BaseGeometry

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

_GEOMETRY_ERRORS = (shapely.errors.ShapelyError, ValueError, TypeError)


def _pin_from_geometry(geometry: BaseGeometry, index: int, source_label: str, user_profile: Profile) -> dict[str, Any] | None:
    """Build a pin dict from *geometry*'s centroid, or None if it has no location."""
    if geometry is None or geometry.is_empty:
        return None
    centroid = geometry.centroid
    if centroid.is_empty:
        return None
    return {
        "latitude": centroid.y,
        "longitude": centroid.x,
        "profile": user_profile,
        "name": f"Imported {geometry.geom_type} {index}",
        "description": f"{geometry.geom_type} geometry imported from {source_label}",
    }


@untrusted_parse("geo.wkt")
def wkt_to_dict(file_contents: bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a WKT file (one geometry per line) into pin dicts.

    Args:
        file_contents: The file, UTF-8 text, as bytes or a binary file.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per valid geometry line.

    Raises:
        UnicodeDecodeError: If the file is not UTF-8 text."""
    pins = list(iter_wkt_pins(file_contents, user_profile))
    logger.debug("Converted %s geometries from WKT file to pins.", len(pins))
    return pins


@untrusted_parse("geo.wkt")
def iter_wkt_pins(file_contents: bytes | IO[bytes], user_profile: Profile) -> Iterator[dict[str, Any]]:
    """:func:`wkt_to_dict`, one line at a time, reading no more of the file than that takes.

    Args:
        file_contents: The file, UTF-8 text, as bytes or a binary file.
        user_profile: The profile to associate with each pin.

    Yields:
        One pin dict per valid geometry line.

    Raises:
        UnicodeDecodeError: If the file is not UTF-8 text, once reading reaches the bad byte."""
    for line_number, raw_line in enumerate(iter_lines(iter_decoded(as_stream(file_contents), "utf-8")), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            geometry = shapely.wkt.loads(line)
        except _GEOMETRY_ERRORS as exc:
            logger.warning("Skipping invalid WKT on line %s: %s", line_number, exc)
            continue

        pin = _pin_from_geometry(geometry, line_number, "WKT", user_profile)
        if pin is not None:
            yield pin


@untrusted_parse("geo.wkb")
def wkb_to_dict(file_contents: bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a WKB file into pin dicts.

    Args:
        file_contents: The file, as bytes or a seekable binary file positioned at its start.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per valid geometry."""
    pins = list(iter_wkb_pins(file_contents, user_profile))
    logger.debug("Converted %s geometries from WKB file to pins.", len(pins))
    return pins


@untrusted_parse("geo.wkb")
def iter_wkb_pins(file_contents: bytes | IO[bytes], user_profile: Profile) -> Iterator[dict[str, Any]]:
    """:func:`wkb_to_dict`, one line at a time, reading no more of the file than that takes.

    A file that is not ASCII is one raw binary geometry, and is read whole.

    Args:
        file_contents: The file, as bytes or a seekable binary file positioned at its start.
        user_profile: The profile to associate with each pin.

    Yields:
        One pin dict per valid geometry.

    Raises:
        shapely.errors.ShapelyError: A binary WKB file is not a valid geometry."""
    stream = as_stream(file_contents)
    start = stream.tell()
    if not is_valid_text(stream, "ascii"):
        # Not hex text - treat the whole file as one raw binary WKB geometry.
        stream.seek(start)
        try:
            geometry = shapely.wkb.loads(stream.read())
        except _GEOMETRY_ERRORS as exc:
            logger.exception("Failed to import pins from binary WKB: %s", exc)
            raise
        pin = _pin_from_geometry(geometry, 1, "binary WKB", user_profile)
        if pin is not None:
            yield pin
        return

    stream.seek(start)
    for line_number, raw_line in enumerate(iter_lines(iter_decoded(stream, "ascii")), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            geometry = shapely.wkb.loads(bytes.fromhex(line))
        except _GEOMETRY_ERRORS as exc:
            logger.warning("Skipping invalid hex WKB on line %s: %s", line_number, exc)
            continue

        pin = _pin_from_geometry(geometry, line_number, "hex WKB", user_profile)
        if pin is not None:
            yield pin
