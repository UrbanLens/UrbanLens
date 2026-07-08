"""GPX track/route import - the Route counterpart to gpx.py's waypoint-only pin import.

gpx.py intentionally ignores ``<trk>``/``<rte>`` content when producing pins (see
its module docstring). This module handles that content instead, turning each
track/route into a Route record rather than a flood of individual pins.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, NamedTuple

import gpxpy
import gpxpy.gpx

from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# A pin's dwell must last at least this long, within DWELL_RADIUS_M of the pin,
# for a GEOLOCATION visit to be created from it.
DWELL_RADIUS_M = 50
DWELL_MINIMUM_MINUTES = 10


class RawTrackPoint(NamedTuple):
    """A single GPS point retained for dwell-detection, alongside its timestamp."""

    latitude: float
    longitude: float
    time: datetime | None


class ParsedRoute(NamedTuple):
    """An unsaved Route paired with its raw points (needed for dwell-detection)."""

    route: Route
    raw_points: list[RawTrackPoint]


def _track_points(track: gpxpy.gpx.GPXTrack) -> list[RawTrackPoint]:
    """Flatten every segment of a GPX track into a single point list."""
    points: list[RawTrackPoint] = []
    for segment in track.segments:
        for point in segment.points:
            if point.latitude is None or point.longitude is None:
                continue
            points.append(RawTrackPoint(point.latitude, point.longitude, point.time))
    return points


def _route_points(route: gpxpy.gpx.GPXRoute) -> list[RawTrackPoint]:
    """Return a GPX route's points (route points don't have per-segment nesting)."""
    points: list[RawTrackPoint] = []
    for point in route.points:
        if point.latitude is None or point.longitude is None:
            continue
        points.append(RawTrackPoint(point.latitude, point.longitude, point.time))
    return points


def _elevation_gain_loss(track: gpxpy.gpx.GPXTrack) -> tuple[float | None, float | None]:
    """Return (gain, loss) in meters summed across a track's segments, or (None, None)."""
    gain = 0.0
    loss = 0.0
    found = False
    for segment in track.segments:
        uphill_downhill = segment.get_uphill_downhill()
        if uphill_downhill.uphill or uphill_downhill.downhill:
            found = True
        gain += uphill_downhill.uphill or 0.0
        loss += uphill_downhill.downhill or 0.0
    return (gain, loss) if found else (None, None)


def _started_ended_at(points: list[RawTrackPoint]) -> tuple[datetime | None, datetime | None]:
    """Return (first, last) timestamp among points that carry one, or (None, None)."""
    timestamps = [p.time for p in points if p.time is not None]
    if not timestamps:
        return None, None
    return timestamps[0], timestamps[-1]


def _build_route(
    *,
    profile: Profile,
    source: str,
    source_filename: str,
    name: str,
    points: list[RawTrackPoint],
    elevation_gain: float | None = None,
    elevation_loss: float | None = None,
) -> ParsedRoute | None:
    """Build an unsaved Route from a raw point list, or None if too few points."""
    if len(points) < 2:
        return None

    geometry = simplify_and_measure([(p.latitude, p.longitude) for p in points])
    started_at, ended_at = _started_ended_at(points)

    route = Route(
        profile=profile,
        name=name,
        source=source,
        source_filename=source_filename,
        path=geometry.path,
        raw_point_count=geometry.raw_point_count,
        simplified_point_count=geometry.simplified_point_count,
        distance_meters=geometry.distance_meters,
        elevation_gain_meters=elevation_gain,
        elevation_loss_meters=elevation_loss,
        started_at=started_at,
        ended_at=ended_at,
    )
    return ParsedRoute(route=route, raw_points=points)


def gpx_tracks_to_routes(file_contents: bytes, user_profile: Profile, source_filename: str) -> list[ParsedRoute]:
    """Parse every ``<trk>`` and ``<rte>`` in a GPX file into unsaved Route instances.

    Args:
        file_contents: Raw GPX file bytes.
        user_profile: Owning profile for the created Route rows.
        source_filename: Original upload filename, stored as Route.source_filename.

    Returns:
        List of ParsedRoute (unsaved Route + its raw points) - one per
        ``<trk>``/``<rte>`` element with at least 2 points.

    Raises:
        gpxpy.gpx.GPXException: If the file is not valid GPX.
        UnicodeDecodeError: If the file is not UTF-8 text.
    """
    text = file_contents.decode("utf-8")
    gpx = gpxpy.parse(text)

    parsed: list[ParsedRoute] = []

    for track in gpx.tracks:
        points = _track_points(track)
        gain, loss = _elevation_gain_loss(track)
        result = _build_route(
            profile=user_profile,
            source=RouteSource.GPX_TRACK,
            source_filename=source_filename,
            name=(track.name or "").strip(),
            points=points,
            elevation_gain=gain,
            elevation_loss=loss,
        )
        if result:
            parsed.append(result)

    for route in gpx.routes:
        points = _route_points(route)
        result = _build_route(
            profile=user_profile,
            source=RouteSource.GPX_ROUTE,
            source_filename=source_filename,
            name=(route.name or "").strip(),
            points=points,
        )
        if result:
            parsed.append(result)

    logger.debug(
        "Converted %s tracks/routes from GPX file '%s' to Route candidates.",
        len(parsed),
        source_filename,
    )
    return parsed


def detect_dwells_and_create_visits(route: Route, raw_points: list[RawTrackPoint], profile: Profile) -> int:
    """Scan a route's raw points for dwells near the profile's own pins and create visits.

    A bounded scan, not a clustering algorithm: the profile's pins near the
    route's path are found once, then the raw points are walked a single time
    checking whether any contiguous run stays within DWELL_RADIUS_M of a
    candidate pin for at least DWELL_MINIMUM_MINUTES.

    Args:
        route: The already-saved Route these points belong to.
        raw_points: The route's raw (pre-simplification) points, in order.
        profile: Owning profile - only this profile's own pins are candidates.

    Returns:
        Number of PinVisit(source=GEOLOCATION) rows created.
    """
    from django.contrib.gis.measure import D
    from geopy.distance import geodesic

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
    from urbanlens.dashboard.services.visits import sync_last_visited

    if not any(p.time is not None for p in raw_points):
        # No per-point timestamps (e.g. some <rte> files) - dwell duration can't be measured.
        return 0

    candidate_pins = list(
        Pin.objects.filter(
            profile=profile,
            location__point__distance_lte=(route.path, D(m=DWELL_RADIUS_M)),
        ).select_related("location"),
    )
    if not candidate_pins:
        return 0

    minimum_dwell = timedelta(minutes=DWELL_MINIMUM_MINUTES)
    created = 0

    for pin in candidate_pins:
        pin_coords = (pin.point.y, pin.point.x)
        dwell_start: datetime | None = None
        last_in_range_time: datetime | None = None
        qualified = False

        for point in raw_points:
            if point.time is None:
                continue

            in_range = geodesic(pin_coords, (point.latitude, point.longitude)).meters <= DWELL_RADIUS_M
            if in_range:
                if dwell_start is None:
                    dwell_start = point.time
                last_in_range_time = point.time
            elif dwell_start is not None:
                if last_in_range_time and (last_in_range_time - dwell_start) >= minimum_dwell:
                    qualified = True
                    break
                dwell_start = None
                last_in_range_time = None

        if not qualified and dwell_start is not None and last_in_range_time and (last_in_range_time - dwell_start) >= minimum_dwell:
            qualified = True

        if qualified and dwell_start is not None:
            _, was_created = PinVisit.objects.get_or_create(
                pin=pin,
                visited_at=dwell_start,
                source=VisitSource.GEOLOCATION,
                defaults={"route": route},
            )
            if was_created:
                sync_last_visited(pin)
                created += 1

    return created
