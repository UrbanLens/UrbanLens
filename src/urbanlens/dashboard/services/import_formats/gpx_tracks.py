"""GPX track/route import - the Route counterpart to gpx.py's waypoint-only pin import."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import math
from typing import IO, TYPE_CHECKING, Any, NamedTuple
from xml.etree.ElementTree import Element  # nosec B405 - builds a holder, parses nothing

from defusedxml.ElementTree import iterparse as iterparse_xml_defused
from django.contrib.gis.geos import LineString
import gpxpy.geo
import gpxpy.gpx
from gpxpy.gpxfield import GPXComplexField

from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.services.core.numbers import LATITUDE_BOUND, LONGITUDE_BOUND, coordinate_or_none
from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure
from urbanlens.dashboard.services.import_formats.streams import as_stream, iter_decoded
from urbanlens.dashboard.services.sandbox import untrusted_parse

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


def track_point(latitude: object, longitude: object, time: datetime | None) -> RawTrackPoint | None:
    """A track point, or None when its coordinates are not a place on the globe.

    Args:
        latitude: The point's latitude in degrees, as parsed.
        longitude: Its longitude.
        time: When it was recorded, if known.

    Returns:
        The point, or None for a coordinate that is not a finite number in range.
    """
    lat = coordinate_or_none(latitude, bound=LATITUDE_BOUND)
    lng = coordinate_or_none(longitude, bound=LONGITUDE_BOUND)
    return RawTrackPoint(lat, lng, time) if lat is not None and lng is not None else None


class ParsedRoute(NamedTuple):
    """An unsaved Route paired with its raw points (needed for dwell-detection)."""

    route: Route
    raw_points: list[RawTrackPoint]

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable form, without the profile, for :meth:`from_json` to rebuild.

        Raw points are kept only when one carries a timestamp, since dwell detection needs nothing else.

        Returns:
            The route's fields, its simplified path as ``[lng, lat]`` pairs, and ``points`` as
            ``[lat, lng, iso-timestamp | None]`` triples.
        """
        route = self.route
        timed = any(point.time is not None for point in self.raw_points)
        return {
            "name": route.name,
            "source": route.source,
            "source_filename": route.source_filename,
            "path": [list(coordinate) for coordinate in route.path.coords],
            "raw_point_count": route.raw_point_count,
            "simplified_point_count": route.simplified_point_count,
            "distance_meters": route.distance_meters,
            "elevation_gain_meters": route.elevation_gain_meters,
            "elevation_loss_meters": route.elevation_loss_meters,
            "started_at": _isoformat(route.started_at),
            "ended_at": _isoformat(route.ended_at),
            "points": [[point.latitude, point.longitude, _isoformat(point.time)] for point in self.raw_points] if timed else [],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], profile: Profile) -> ParsedRoute:
        """Rebuild an unsaved Route for *profile* from :meth:`to_json`'s output.

        Args:
            data: One serialised route.
            profile: The route's owner.

        Returns:
            The route and its raw points.

        Raises:
            ValueError: *data* is not a serialised route.
            TypeError: *data* is not a serialised route.
            KeyError: *data* is not a serialised route.
        """
        route = Route(
            profile=profile,
            name=str(data["name"])[:255],
            source=RouteSource(data["source"]),
            source_filename=str(data["source_filename"])[:255],
            path=LineString([(float(lng), float(lat)) for lng, lat in data["path"]], srid=4326),
            raw_point_count=int(data["raw_point_count"]),
            simplified_point_count=int(data["simplified_point_count"]),
            distance_meters=float(data["distance_meters"]),
            elevation_gain_meters=_optional_float(data["elevation_gain_meters"]),
            elevation_loss_meters=_optional_float(data["elevation_loss_meters"]),
            started_at=_parse_datetime(data["started_at"]),
            ended_at=_parse_datetime(data["ended_at"]),
        )
        points = [RawTrackPoint(float(lat), float(lng), _parse_datetime(time)) for lat, lng, time in data["points"]]
        return cls(route=route, raw_points=points)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _optional_float(value: float | None) -> float | None:
    return float(value) if value is not None else None


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


@untrusted_parse("geo.gpx")
def gpx_tracks_to_routes(file_contents: bytes | IO[bytes], user_profile: Profile, source_filename: str) -> list[ParsedRoute]:
    """Parse every ``<trk>`` and ``<rte>`` in a GPX file into unsaved Route instances.

    Args:
        file_contents: The GPX file, as bytes or a seekable binary file positioned at its start.
        user_profile: Owning profile for the created Route rows.
        source_filename: Original upload filename, stored as Route.source_filename.

    Returns:
        List of ParsedRoute (unsaved Route + its raw points) - one per ``<trk>``/``<rte>`` element with at least 2 points.

    Raises:
        gpxpy.gpx.GPXException: If the file is not valid GPX.
        UnicodeDecodeError: If the file is not UTF-8 text.
        defusedxml.ElementTree.ParseError: If the file is not well-formed XML.
        ValueError: If the XML attempts a forbidden DTD/entity-expansion/ external-entity reference (an XXE attempt)."""
    parsed = read_gpx(file_contents, user_profile, source_filename, max_waypoints=0).routes
    logger.debug(
        "Converted %s tracks/routes from GPX file '%s' to Route candidates.",
        len(parsed),
        source_filename,
    )
    return parsed


@dataclass
class GpxContents:
    """What a GPX file holds for an import.

    Attributes:
        waypoints: One pin dict per ``<wpt>``, in document order.
        routes: One ParsedRoute per ``<trk>`` with at least 2 points, then one per such ``<rte>``.
    """

    waypoints: list[dict[str, Any]] = field(default_factory=list)
    routes: list[ParsedRoute] = field(default_factory=list)


@untrusted_parse("geo.gpx")
def read_gpx(file_contents: bytes | IO[bytes], user_profile: Profile, source_filename: str, *, max_waypoints: int | None = None, routes: bool = True) -> GpxContents:
    """Read a GPX file's waypoints, tracks and routes in one pass, an element at a time.

    Each ``wpt``, ``trkpt`` and ``rtept`` is read by gpxpy's own field parser as it closes, then freed, so the file is
    never held as a tree. defusedxml reads the file, so gpxpy never parses untrusted XML itself.

    Args:
        file_contents: The GPX file, as bytes or a seekable binary file positioned at its start.
        user_profile: Owning profile for the routes, and the profile each waypoint pin is for.
        source_filename: Original upload filename, stored as Route.source_filename.
        max_waypoints: The most waypoint pins to keep; every one when None.
        routes: Whether to build the routes; the tracks and routes are validated either way.

    Returns:
        The waypoints and routes.

    Raises:
        gpxpy.gpx.GPXException: If the file is not valid GPX.
        UnicodeDecodeError: If the file is not UTF-8 text.
        defusedxml.ElementTree.ParseError: If the file is not well-formed XML.
        ValueError: If the XML attempts a forbidden entity-expansion or external-entity reference (an XXE attempt)."""
    stream = as_stream(file_contents)
    start = stream.tell()
    for _ in iter_decoded(stream, "utf-8"):
        pass
    stream.seek(start)
    reader = _GpxReader(user_profile, source_filename, max_waypoints=max_waypoints, routes=routes)
    for event, item in iterparse_xml_defused(stream, events=("start-ns", "start", "end")):
        reader.feed(event, item)
    return GpxContents(waypoints=reader.waypoints, routes=reader.tracks + reader.routes)


def _read_fields[T](cls: type[T], element: Element, version: str) -> T:
    """Read one element into a gpxpy object with gpxpy's own field parser, as gpxpy reads each element of its tree.

    Args:
        cls: The gpxpy class the element is, e.g. ``GPXTrackPoint``.
        element: The element.
        version: The root's ``version``.

    Returns:
        The object, its list fields empty for the children already read and removed.

    Raises:
        gpxpy.gpx.GPXException: A field gpxpy would refuse.
    """
    # A complex field finds the element in a parent; gpxfield.gpx_fields_from_xml, which it calls, types the element
    # as a str.
    holder = Element("holder")
    holder.append(element)
    return GPXComplexField(cls.__name__, classs=cls, tag=element.tag).from_xml(holder, version)


class _GpxReader:
    """Builds :class:`GpxContents` from iterparse events, keeping only the elements still open."""

    def __init__(self, user_profile: Profile, source_filename: str, *, max_waypoints: int | None, routes: bool) -> None:
        self.user_profile = user_profile
        self.source_filename = source_filename
        self.max_waypoints = max_waypoints
        self.build_routes = routes
        self.waypoints: list[dict[str, Any]] = []
        self.tracks: list[ParsedRoute] = []
        self.routes: list[ParsedRoute] = []
        self._stack: list[Element] = []
        # gpxpy reads the 1.1 fields when the root says version 1.1, and the 1.0 fields otherwise.
        self._version = ""
        # gpxpy strips the document's first default namespace declaration before it builds its tree.
        self._default_namespace: str | None = None
        self._points: list[RawTrackPoint] = []
        self._elevations: list[float | None] = []
        self._gain = 0.0
        self._loss = 0.0
        self._climbed = False

    def feed(self, event: str, item: Any) -> None:
        """Take one iterparse event.

        Args:
            event: ``"start-ns"``, ``"start"`` or ``"end"``.
            item: The namespace declaration's ``(prefix, uri)``, or the element.
        """
        if event == "start-ns":
            prefix, uri = item
            if not prefix and self._default_namespace is None:
                self._default_namespace = uri
            return
        if event == "start":
            if self._default_namespace is not None and item.tag.startswith(f"{{{self._default_namespace}}}"):
                item.tag = item.tag[len(self._default_namespace) + 2 :]
            if not self._stack:
                self._version = item.get("version") or ""
            self._stack.append(item)
            return
        self._stack.pop()
        self._close(item, [element.tag for element in self._stack[1:]])

    def _close(self, element: Element, ancestors: list[str]) -> None:
        if not self._stack:
            # The root's own fields, which gpxpy validates too, once every waypoint, track and route is gone.
            _read_fields(gpxpy.gpx.GPX, element, self._version)
            return
        if not ancestors and element.tag == "wpt":
            self._waypoint(_read_fields(gpxpy.gpx.GPXWaypoint, element, self._version))
        elif ancestors == ["trk", "trkseg"] and element.tag == "trkpt":
            point = _read_fields(gpxpy.gpx.GPXTrackPoint, element, self._version)
            if self.build_routes and (kept := track_point(point.latitude, point.longitude, point.time)):
                self._points.append(kept)
                self._elevations.append(point.elevation if point.elevation is None or math.isfinite(point.elevation) else None)
        elif ancestors == ["trk"] and element.tag == "trkseg":
            _read_fields(gpxpy.gpx.GPXTrackSegment, element, self._version)
            self._segment_ended()
        elif not ancestors and element.tag == "trk":
            track = _read_fields(gpxpy.gpx.GPXTrack, element, self._version)
            self._route_ended(self.tracks, RouteSource.GPX_TRACK, track.name, climb=True)
        elif ancestors == ["rte"] and element.tag == "rtept":
            route_point = _read_fields(gpxpy.gpx.GPXRoutePoint, element, self._version)
            if self.build_routes and (kept := track_point(route_point.latitude, route_point.longitude, route_point.time)):
                self._points.append(kept)
        elif not ancestors and element.tag == "rte":
            route = _read_fields(gpxpy.gpx.GPXRoute, element, self._version)
            self._route_ended(self.routes, RouteSource.GPX_ROUTE, route.name, climb=False)
        else:
            return
        self._stack[-1].remove(element)

    def _waypoint(self, waypoint: gpxpy.gpx.GPXWaypoint) -> None:
        if self.max_waypoints is not None and len(self.waypoints) >= self.max_waypoints:
            return
        description_parts = [part.strip() for part in (waypoint.description, waypoint.comment) if part and part.strip()]
        if waypoint.elevation is not None:
            description_parts.append(f"Elevation: {waypoint.elevation:.1f}m")
        if waypoint.time is not None:
            description_parts.append(f"Recorded: {waypoint.time.isoformat()}")
        self.waypoints.append(
            {
                "latitude": waypoint.latitude,
                "longitude": waypoint.longitude,
                "profile": self.user_profile,
                "name": (waypoint.name or "Unnamed waypoint").strip(),
                "description": " | ".join(description_parts),
            },
        )

    def _segment_ended(self) -> None:
        # GPXTrackSegment.get_uphill_downhill, summed over a track's segments.
        uphill, downhill = gpxpy.geo.calculate_uphill_downhill(self._elevations) if self._elevations else (0, 0)
        self._climbed = self._climbed or bool(uphill or downhill)
        self._gain += uphill or 0.0
        self._loss += downhill or 0.0
        self._elevations = []

    def _route_ended(self, into: list[ParsedRoute], source: str, name: str | None, *, climb: bool) -> None:
        gain, loss = (self._gain, self._loss) if climb and self._climbed else (None, None)
        if self.build_routes and (
            result := _build_route(
                profile=self.user_profile,
                source=source,
                source_filename=self.source_filename,
                name=(name or "").strip(),
                points=self._points,
                elevation_gain=gain,
                elevation_loss=loss,
            )
        ):
            into.append(result)
        self._points = []
        self._gain = self._loss = 0.0
        self._climbed = False


def detect_dwells_and_create_visits(route: Route, raw_points: list[RawTrackPoint], profile: Profile) -> int:
    """Scan a route's raw points for dwells near the profile's own pins and create visits.

    Args:
        route: The already-saved Route these points belong to.
        raw_points: The route's raw (pre-simplification) points, in order.
        profile: Owning profile - only this profile's own pins are candidates.

    Returns:
        Zero when visit logging is turned off, even though the route itself still saves."""
    from django.contrib.gis.measure import D
    from django.db import transaction
    from geopy.distance import geodesic

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
    from urbanlens.dashboard.services.visits.visits import sync_last_visited, visit_logging_allowed

    if not visit_logging_allowed(profile):
        return 0

    if not any(p.time is not None for p in raw_points):
        # No per-point timestamps (e.g. some <rte> files) - dwell duration can't be measured.
        return 0

    candidate_pins = list(
        Pin.objects.filter(
            profile=profile,
            location__point__dwithin=(route.path, D(m=DWELL_RADIUS_M)),
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
            # GEOLOCATION means "the user's device provided a geolocation" - a live ping, gated by
            with transaction.atomic():
                Pin.objects.select_for_update().get(pk=pin.pk)
                _, was_created = PinVisit.objects.get_or_create(
                    pin=pin,
                    visited_at=dwell_start,
                    source=VisitSource.HISTORY,
                    defaults={"route": route},
                )
            if was_created:
                sync_last_visited(pin)
                created += 1

    return created
