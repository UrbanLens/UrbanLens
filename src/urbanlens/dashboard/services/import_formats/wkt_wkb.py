"""WKT/WKB pin import.

Unlike the other formats, a WKT/WKB file is treated as N independent one-line
records rather than a single all-or-nothing document: a malformed line is skipped
with a warning instead of failing the whole file, since these are typically hand-
pasted or hand-edited rather than produced by a single trusted export pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import shapely.errors
import shapely.wkb
import shapely.wkt

if TYPE_CHECKING:
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


def wkt_to_dict(file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a WKT file (one geometry per line) into pin dicts.

    Args:
        file_contents: Raw file bytes, expected to be UTF-8 text.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per valid geometry line. Lines that fail to parse
        are skipped with a warning rather than aborting the whole file.

    Raises:
        UnicodeDecodeError: If the file is not UTF-8 text.
    """
    text = file_contents.decode("utf-8")
    pins: list[dict[str, Any]] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
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
            pins.append(pin)

    logger.debug("Converted %s geometries from WKT file to pins.", len(pins))
    return pins


def wkb_to_dict(file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a WKB file into pin dicts.

    Supports two forms:

    - Raw binary WKB: the entire file is treated as a single geometry.
    - Hex-encoded WKB text (e.g. copy-pasted from ``ST_AsBinary``/``ST_AsHexWKB``
      in a PostGIS client): one hex-encoded geometry per line.

    Args:
        file_contents: Raw file bytes.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per valid geometry.
    """
    pins: list[dict[str, Any]] = []

    try:
        text = file_contents.decode("ascii")
    except UnicodeDecodeError:
        # Not hex text - treat the whole file as one raw binary WKB geometry.
        try:
            geometry = shapely.wkb.loads(file_contents)
        except _GEOMETRY_ERRORS as exc:
            logger.exception("Failed to import pins from binary WKB: %s", exc)
            raise
        pin = _pin_from_geometry(geometry, 1, "binary WKB", user_profile)
        if pin is not None:
            pins.append(pin)
        return pins

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
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
            pins.append(pin)

    logger.debug("Converted %s geometries from WKB file to pins.", len(pins))
    return pins
