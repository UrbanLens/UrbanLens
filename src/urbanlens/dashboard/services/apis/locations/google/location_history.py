"""Google Takeout Semantic Location History importer."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from django.db import DatabaseError

from urbanlens.dashboard.services.core.numbers import LATITUDE_BOUND, LONGITUDE_BOUND, coordinate_or_none

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

logger = logging.getLogger(__name__)

VISIT_MATCH_RADIUS_M = 100
MIN_CONFIDENCE = 50


def _e7_degrees(value: object, *, bound: float) -> float | None:
    """A Takeout ``latitudeE7``-style coordinate in degrees, or None when it is not a place on the globe.

    Args:
        value: The integer the file holds: degrees times ten million.
        bound: :data:`LATITUDE_BOUND` or :data:`LONGITUDE_BOUND`.

    Returns:
        The degrees, or None for anything but a number within *bound* once scaled.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return coordinate_or_none(value / 1e7, bound=bound)
    except OverflowError:
        # An integer past a float's range.
        return None


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


def parse_semantic_visits(json_data: dict) -> Generator[dict[str, Any], None, None]:
    """Yield one visit dict per qualifying ``placeVisit`` in a timeline JSON.

    Args:
        json_data: Parsed Semantic Location History dict containing ``timelineObjects``.

    Yields:
        Dict with keys: ``latitude``, ``longitude``, ``visited_at`` (tz-aware datetime), ``place_name`` (str), ``place_id`` (str|None), ``confidence`` (int)."""
    for obj in json_data.get("timelineObjects", []):
        visit = semantic_visit(obj)
        if visit is not None:
            yield visit


def semantic_visit(obj: dict) -> dict[str, Any] | None:
    """The visit one ``timelineObjects`` entry records, if it is a qualifying ``placeVisit``.

    Args:
        obj: One entry of a Semantic Location History file's ``timelineObjects``.

    Returns:
        A dict shaped as :func:`parse_semantic_visits` yields, or None.
    """
    pv = obj.get("placeVisit")
    if not pv:
        return None
    confidence = pv.get("visitConfidence", 100)
    if confidence < MIN_CONFIDENCE:
        return None
    loc = pv.get("location", {})
    latitude = _e7_degrees(loc.get("latitudeE7"), bound=LATITUDE_BOUND)
    longitude = _e7_degrees(loc.get("longitudeE7"), bound=LONGITUDE_BOUND)
    if latitude is None or longitude is None:
        return None
    start_ts = (pv.get("duration") or {}).get("startTimestamp")
    if not start_ts:
        return None
    try:
        visited_at = datetime.fromisoformat(start_ts)
    except ValueError:
        logger.debug("Unparseable placeVisit timestamp: %s", start_ts)
        return None
    return {
        "latitude": latitude,
        "longitude": longitude,
        "visited_at": visited_at,
        "place_name": loc.get("name", ""),
        "place_id": loc.get("placeId"),
        "confidence": confidence,
    }


def iter_location_history_events(
    visits: list[dict[str, Any]],
    profile: Profile,
    radius_m: int = VISIT_MATCH_RADIUS_M,
) -> Iterator[dict[str, Any]]:
    """Log each place visit as a history visit to the profile's nearest pin, one event per whole percent.

    Args:
        visits: Dicts shaped like :func:`parse_semantic_visits`'s, with ``latitude``, ``longitude`` and a
            tz-aware ``visited_at``.
        profile: The profile whose pins are matched.
        radius_m: Match radius in metres.

    Yields:
        ``{type, subtype: "location_history", ...}``: ``start`` with ``total``; ``progress`` with
        ``current``, ``total``, ``percent``, ``matched`` and ``skipped``; then ``complete`` with the
        final counts, or a lone ``error`` with a ``message``.
    """
    from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
    from urbanlens.dashboard.services.visits.visits import find_nearest_pin, visit_logging_allowed

    subtype = "location_history"
    if not visit_logging_allowed(profile):
        yield {"type": "error", "message": "Visit logging is turned off - enable it in Settings to import your location history.", "subtype": subtype}
        return

    total = len(visits)
    if not total:
        return
    yield {"type": "start", "total": total, "subtype": subtype}

    matched = 0
    skipped = 0

    # A Takeout export is mostly the same handful of everyday coordinates repeated thousands of
    # times, and each distinct one costs a PostGIS nearest-neighbour query.
    nearest_pin_memo: dict[tuple[float, float], Pin | None] = {}

    # Seeded from what is already stored, then kept current, so a duplicate within one file is skipped too.
    seen_visits: set[tuple[int, datetime]] = set(
        PinVisit.objects.filter(pin__profile=profile, source=VisitSource.HISTORY).values_list("pin_id", "visited_at"),
    )

    last_percent = -1

    for i, visit in enumerate(visits, 1):
        coordinates = (visit["latitude"], visit["longitude"])
        if coordinates in nearest_pin_memo:
            pin = nearest_pin_memo[coordinates]
        else:
            pin = find_nearest_pin(visit["latitude"], visit["longitude"], profile, radius_m)
            nearest_pin_memo[coordinates] = pin
        if pin is not None and (pin.pk, visit["visited_at"]) not in seen_visits:
            try:
                PinVisit.objects.create(pin=pin, visited_at=visit["visited_at"], source=VisitSource.HISTORY)
                seen_visits.add((pin.pk, visit["visited_at"]))
                if not pin.last_visited or visit["visited_at"] > pin.last_visited:
                    pin.last_visited = visit["visited_at"]
                    pin.save(update_fields=["last_visited"])
                matched += 1
            except DatabaseError as exc:
                logger.warning("Failed to save visit for pin %s: %s", pin.id, exc)
                skipped += 1
        else:
            skipped += 1

        percent = min(100, int(i / total * 100))
        if percent != last_percent or i in (1, total):
            last_percent = percent
            yield {"type": "progress", "current": i, "total": total, "percent": percent, "matched": matched, "skipped": skipped, "subtype": subtype}

    yield {"type": "complete", "total": total, "matched": matched, "skipped": skipped, "subtype": subtype}


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string, returning None if absent/unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.debug("Unparseable activitySegment timestamp: %s", value)
        return None


def _activity_segment_points(segment: dict) -> Generator[Any, None, None]:
    """Yield RawTrackPoint entries for an activitySegment, preferring the timestamped path.
    ``simplifiedRawPath.points`` carries a per-point timestamp when Google recorded one; ``waypointPath.waypoints`` is a coarser fallback with only coordinates, so points from it carry no timestamp."""
    from urbanlens.dashboard.services.import_formats.gpx_tracks import track_point

    simplified_points = (segment.get("simplifiedRawPath") or {}).get("points") or []
    if simplified_points:
        for point in simplified_points:
            track = track_point(_e7_degrees(point.get("latE7"), bound=LATITUDE_BOUND), _e7_degrees(point.get("lngE7"), bound=LONGITUDE_BOUND), _parse_iso_timestamp(point.get("timestamp")))
            if track is not None:
                yield track
        return

    for waypoint in (segment.get("waypointPath") or {}).get("waypoints") or []:
        track = track_point(_e7_degrees(waypoint.get("latE7"), bound=LATITUDE_BOUND), _e7_degrees(waypoint.get("lngE7"), bound=LONGITUDE_BOUND), None)
        if track is not None:
            yield track


def _activity_segment(obj: dict) -> dict[str, Any] | None:
    """The route one ``timelineObjects`` entry records, if it is an activitySegment with a usable path.

    Args:
        obj: One entry of a Semantic Location History file's ``timelineObjects``.

    Returns:
        Dict with keys: ``points`` (list[RawTrackPoint]), ``started_at``, ``ended_at`` (tz-aware datetime | None), ``distance_meters`` (float | None, Google's own estimate - preferred over recomputing from sparse waypoints), or None."""
    segment = obj.get("activitySegment")
    if not segment:
        return None
    points = list(_activity_segment_points(segment))
    if len(points) < 2:
        return None
    duration = segment.get("duration") or {}
    return {
        "points": points,
        "started_at": _parse_iso_timestamp(duration.get("startTimestamp")),
        "ended_at": _parse_iso_timestamp(duration.get("endTimestamp")),
        "distance_meters": segment.get("distance"),
    }


def semantic_history_to_routes(json_data: dict, profile: Profile, source_filename: str) -> list[ParsedRoute]:
    """Build unsaved Route candidates from a Semantic Location History file's activitySegments.

    Args:
        json_data: Parsed Semantic Location History dict.
        profile: Owning profile for the created Route rows.
        source_filename: Original uploaded filename, stored as Route.source_filename.

    Returns:
        List of ParsedRoute - one per qualifying activitySegment."""
    routes = (semantic_route(obj, profile, source_filename) for obj in json_data.get("timelineObjects", []))
    return [route for route in routes if route is not None]


def semantic_route(obj: dict, profile: Profile, source_filename: str) -> ParsedRoute | None:
    """The unsaved Route candidate one ``timelineObjects`` entry records, if it is a qualifying activitySegment.

    Args:
        obj: One entry of a Semantic Location History file's ``timelineObjects``.
        profile: Owning profile for the created Route row.
        source_filename: Original uploaded filename, stored as Route.source_filename.

    Returns:
        The route and its raw points, or None."""
    from urbanlens.dashboard.models.routes.model import Route, RouteSource
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute
    from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure

    segment_data = _activity_segment(obj)
    if segment_data is None:
        return None
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
    return ParsedRoute(route=route, raw_points=points)
