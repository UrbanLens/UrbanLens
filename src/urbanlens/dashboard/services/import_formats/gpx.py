"""GPX (waypoints/tracks/routes) pin import.

Only ``<wpt>`` waypoints are imported as pins. Tracks and routes are recorded paths
(a hiking app logs a trackpoint every few seconds), not points of interest - importing
every trackpoint would flood the map with GPS breadcrumbs rather than useful pins, so
``<trk>``/``<rte>`` content is intentionally ignored.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import gpxpy
import gpxpy.gpx

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


def gpx_to_dict(file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
    """Convert a GPX file's waypoints into pin dicts.

    Args:
        file_contents: Raw GPX file bytes.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts with keys ``latitude``, ``longitude``, ``profile``,
        ``name``, ``description``.

    Raises:
        gpxpy.gpx.GPXException: If the file is not valid GPX.
        UnicodeDecodeError: If the file is not UTF-8 text.
    """
    pins: list[dict[str, Any]] = []
    try:
        text = file_contents.decode("utf-8")
        gpx = gpxpy.parse(text)

        for waypoint in gpx.waypoints:
            if waypoint.latitude is None or waypoint.longitude is None:
                continue

            description_parts = [part.strip() for part in (waypoint.description, waypoint.comment) if part and part.strip()]
            if waypoint.elevation is not None:
                description_parts.append(f"Elevation: {waypoint.elevation:.1f}m")
            if waypoint.time is not None:
                description_parts.append(f"Recorded: {waypoint.time.isoformat()}")

            pins.append(
                {
                    "latitude": waypoint.latitude,
                    "longitude": waypoint.longitude,
                    "profile": user_profile,
                    "name": (waypoint.name or "Unnamed waypoint").strip(),
                    "description": " | ".join(description_parts),
                },
            )

        logger.debug("Converted %s waypoints from GPX file to pins (tracks/routes skipped).", len(pins))
    except (gpxpy.gpx.GPXException, UnicodeDecodeError, ValueError) as e:
        logger.exception("Failed to import pins from GPX: %s", e)
        raise

    return pins
