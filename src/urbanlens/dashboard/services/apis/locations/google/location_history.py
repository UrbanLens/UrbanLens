"""Google Takeout Semantic Location History importer.

Processes the monthly JSON timeline files that Google Takeout places under
``Semantic Location History/YYYY/YYYY_MONTH.json``.  Each ``placeVisit``
entry whose coordinates fall within VISIT_MATCH_RADIUS_M metres of an
existing pin owned by the target profile has a PinVisit record created for
it.  Raw ``Records.json`` GPS-point logs are detected but skipped - they
require clustering that is outside the scope of this import.

Typical usage (called from maps.GoogleMapsGateway.import_pins_streaming):

    from urbanlens.dashboard.services.apis.locations.google.location_history import (
        detect_location_history_format,
        import_location_history_streaming,
    )
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

logger = logging.getLogger(__name__)

VISIT_MATCH_RADIUS_M = 100
MIN_CONFIDENCE = 50


def detect_location_history_format(data: dict) -> str | None:
    """Identify the Google Location History JSON variant.

    Args:
        data: Parsed top-level JSON dict.

    Returns:
        ``'semantic'`` for Semantic Location History (``timelineObjects``),
        ``'raw'`` for raw Records.json (``locations``),
        ``None`` if neither pattern is found.
    """
    if "timelineObjects" in data:
        return "semantic"
    if "locations" in data:
        return "raw"
    return None


def _parse_semantic(json_data: dict) -> Generator[dict[str, Any], None, None]:
    """Yield one visit dict per qualifying ``placeVisit`` in a timeline JSON.

    Entries below MIN_CONFIDENCE or missing required fields are silently
    skipped.

    Args:
        json_data: Parsed Semantic Location History dict containing
            ``timelineObjects``.

    Yields:
        Dict with keys: ``latitude``, ``longitude``, ``visited_at``
        (tz-aware datetime), ``place_name`` (str), ``place_id`` (str|None),
        ``confidence`` (int).
    """
    for obj in json_data.get("timelineObjects", []):
        pv = obj.get("placeVisit")
        if not pv:
            continue
        confidence = pv.get("visitConfidence", 100)
        if confidence < MIN_CONFIDENCE:
            continue
        loc = pv.get("location", {})
        lat_e7 = loc.get("latitudeE7")
        lon_e7 = loc.get("longitudeE7")
        if lat_e7 is None or lon_e7 is None:
            continue
        start_ts = (pv.get("duration") or {}).get("startTimestamp")
        if not start_ts:
            continue
        try:
            visited_at = datetime.fromisoformat(start_ts)
        except ValueError:
            logger.debug("Unparseable placeVisit timestamp: %s", start_ts)
            continue
        yield {
            "latitude": lat_e7 / 1e7,
            "longitude": lon_e7 / 1e7,
            "visited_at": visited_at,
            "place_name": loc.get("name", ""),
            "place_id": loc.get("placeId"),
            "confidence": confidence,
        }


def _nearest_pin(lat: float, lon: float, profile: Profile, radius_m: int) -> Pin | None:
    """Return the closest pin in profile within radius_m metres, or None.

    Args:
        lat: Visit latitude.
        lon: Visit longitude.
        profile: Owner profile - only this profile's pins are searched.
        radius_m: Maximum match distance in metres.

    Returns:
        Nearest matching Pin, or None if no pin is within range.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    point = Point(lon, lat, srid=4326)
    return Pin.objects.filter(location__point__distance_lte=(point, D(m=radius_m)), profile=profile).order_by("location__point").first()


def import_location_history_streaming(
    files: list[tuple[str, bytes]],
    profile: Profile,
    radius_m: int = VISIT_MATCH_RADIUS_M,
) -> Iterator[str]:
    r"""Stream SSE events while importing Google Takeout Semantic Location History.

    Iterates over every ``placeVisit`` in each uploaded timeline file and
    attempts to match it to an existing pin.  On a match a PinVisit row is
    created (idempotent - duplicates are skipped).  ``pin.last_visited`` is
    updated whenever a newer visit is matched.

    SSE event shapes emitted:

    - ``{type: "start",    total, subtype: "location_history"}``
    - ``{type: "progress", current, total, percent, matched, skipped,
          subtype: "location_history"}``
    - ``{type: "complete", total, matched, skipped,
          subtype: "location_history"}``
    - ``{type: "error",    message, subtype: "location_history"}``

    Args:
        files: List of ``(filename, raw_bytes)`` pairs already extracted
               from any archive by the caller.
        profile: The user profile whose pins are used for proximity matching.
        radius_m: Match radius in metres (default 100 m).

    Yields:
        SSE-formatted strings (``data: {...}\\n\\n``).
    """
    from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    all_visits: list[dict[str, Any]] = []
    for filename, raw_bytes in files:
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.debug("Skipping non-JSON file in location history import: %s", filename)
            continue
        fmt = detect_location_history_format(data)
        if fmt == "semantic":
            batch = list(_parse_semantic(data))
            logger.info("Parsed %d place visits from %s", len(batch), filename)
            all_visits.extend(batch)
        elif fmt == "raw":
            logger.info(
                "Skipping raw GPS log (Records.json) - point clustering not supported: %s",
                filename,
            )
        else:
            logger.debug("File is not a location history format: %s", filename)

    if not all_visits:
        yield sse(
            {
                "type": "error",
                "message": "No location history entries found in uploaded files.",
                "subtype": "location_history",
            },
        )
        return

    total = len(all_visits)
    yield sse({"type": "start", "total": total, "subtype": "location_history"})

    matched = 0
    skipped = 0

    for i, visit in enumerate(all_visits, 1):
        pin = _nearest_pin(visit["latitude"], visit["longitude"], profile, radius_m)
        if pin is not None:
            already_exists = PinVisit.objects.filter(
                pin=pin,
                visited_at=visit["visited_at"],
                source=VisitSource.HISTORY,
            ).exists()
            if not already_exists:
                try:
                    PinVisit.objects.create(
                        pin=pin,
                        visited_at=visit["visited_at"],
                        source=VisitSource.HISTORY,
                    )
                    if not pin.last_visited or visit["visited_at"] > pin.last_visited:
                        pin.last_visited = visit["visited_at"]
                        pin.save(update_fields=["last_visited"])
                    matched += 1
                except DatabaseError as exc:
                    logger.warning("Failed to save visit for pin %s: %s", pin.id, exc)
                    skipped += 1
            else:
                skipped += 1
        else:
            skipped += 1

        yield sse(
            {
                "type": "progress",
                "current": i,
                "total": total,
                "percent": min(100, int(i / total * 100)),
                "matched": matched,
                "skipped": skipped,
                "subtype": "location_history",
            },
        )

    yield sse(
        {
            "type": "complete",
            "total": total,
            "matched": matched,
            "skipped": skipped,
            "subtype": "location_history",
        },
    )


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string, returning None if absent/unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.debug("Unparseable activitySegment timestamp: %s", value)
        return None


def _activity_segment_points(segment: dict) -> Generator[Any, None, None]:
    """Yield RawTrackPoint entries for an activitySegment, preferring the timestamped path.

    ``simplifiedRawPath.points`` carries a per-point timestamp when Google
    recorded one; ``waypointPath.waypoints`` is a coarser fallback with only
    coordinates, so points from it carry no timestamp.
    """
    from urbanlens.dashboard.services.import_formats.gpx_tracks import RawTrackPoint

    simplified_points = (segment.get("simplifiedRawPath") or {}).get("points") or []
    if simplified_points:
        for point in simplified_points:
            lat_e7 = point.get("latE7")
            lng_e7 = point.get("lngE7")
            if lat_e7 is None or lng_e7 is None:
                continue
            yield RawTrackPoint(lat_e7 / 1e7, lng_e7 / 1e7, _parse_iso_timestamp(point.get("timestamp")))
        return

    for waypoint in (segment.get("waypointPath") or {}).get("waypoints") or []:
        lat_e7 = waypoint.get("latE7")
        lng_e7 = waypoint.get("lngE7")
        if lat_e7 is None or lng_e7 is None:
            continue
        yield RawTrackPoint(lat_e7 / 1e7, lng_e7 / 1e7, None)


def _parse_activity_segments(json_data: dict) -> Generator[dict[str, Any], None, None]:
    """Yield one route dict per activitySegment that has a usable path.

    Args:
        json_data: Parsed Semantic Location History dict containing ``timelineObjects``.

    Yields:
        Dict with keys: ``points`` (list[RawTrackPoint]), ``started_at``,
        ``ended_at`` (tz-aware datetime | None), ``distance_meters`` (float | None,
        Google's own estimate - preferred over recomputing from sparse waypoints).
    """
    for obj in json_data.get("timelineObjects", []):
        segment = obj.get("activitySegment")
        if not segment:
            continue
        points = list(_activity_segment_points(segment))
        if len(points) < 2:
            continue
        duration = segment.get("duration") or {}
        yield {
            "points": points,
            "started_at": _parse_iso_timestamp(duration.get("startTimestamp")),
            "ended_at": _parse_iso_timestamp(duration.get("endTimestamp")),
            "distance_meters": segment.get("distance"),
        }


def semantic_history_to_routes(json_data: dict, profile: Profile, source_filename: str) -> list[ParsedRoute]:
    """Build unsaved Route candidates from a Semantic Location History file's activitySegments.

    Existing placeVisit -> PinVisit(source=HISTORY) handling is untouched; this
    is an additive read of the same files for their activitySegment entries.

    Args:
        json_data: Parsed Semantic Location History dict.
        profile: Owning profile for the created Route rows.
        source_filename: Original uploaded filename, stored as Route.source_filename.

    Returns:
        List of ParsedRoute - one per qualifying activitySegment.
    """
    from urbanlens.dashboard.models.routes.model import Route, RouteSource
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute
    from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure

    routes: list[ParsedRoute] = []
    for segment_data in _parse_activity_segments(json_data):
        points = segment_data["points"]
        geometry = simplify_and_measure([(p.latitude, p.longitude) for p in points])
        google_distance = segment_data["distance_meters"]
        route = Route(
            profile=profile,
            source=RouteSource.GOOGLE_TAKEOUT_SEMANTIC,
            source_filename=source_filename,
            path=geometry.path,
            raw_point_count=geometry.raw_point_count,
            simplified_point_count=geometry.simplified_point_count,
            distance_meters=float(google_distance) if google_distance is not None else geometry.distance_meters,
            started_at=segment_data["started_at"],
            ended_at=segment_data["ended_at"],
        )
        routes.append(ParsedRoute(route=route, raw_points=points))
    return routes
