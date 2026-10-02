"""GPX (waypoints/tracks/routes) pin import."""

from __future__ import annotations

import logging
from typing import IO, TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError as XMLParseError
import gpxpy.gpx

from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


@untrusted_parse("geo.gpx")
def gpx_to_dict(file_contents: bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a GPX file's waypoints into pin dicts.

    Args:
        file_contents: The GPX file, as bytes or a seekable binary file positioned at its start.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts with keys ``latitude``, ``longitude``, ``profile``, ``name``, ``description``.

    Raises:
        gpxpy.gpx.GPXException: If the file is not valid GPX.
        UnicodeDecodeError: If the file is not UTF-8 text.
        defusedxml.ElementTree.ParseError: If the file is not well-formed XML.
        ValueError: If the XML attempts a forbidden DTD/entity-expansion/ external-entity reference (an XXE attempt)."""
    from urbanlens.dashboard.services.import_formats.gpx_tracks import read_gpx

    try:
        pins = read_gpx(file_contents, user_profile, "", routes=False).waypoints
    except (gpxpy.gpx.GPXException, UnicodeDecodeError, ValueError, XMLParseError) as e:
        logger.exception("Failed to import pins from GPX: %s", e)
        raise

    logger.debug("Converted %s waypoints from GPX file to pins (tracks/routes skipped).", len(pins))
    return pins
