"""Geometry-based detection of which pins a MarkupMap effectively shares."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point

from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.services.core.numbers import degrees_or_none
from urbanlens.dashboard.services.geo.web_mercator import meters_per_pixel

if TYPE_CHECKING:
    from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_METERS_PER_DEGREE_LAT = 111_320.0
_METERS_PER_DEGREE_LNG_AT_EQUATOR = 111_320.0

# Assumed client viewport size in CSS pixels, standing in for the real (unknown) container size -
# MarkupMap only ever persists center+zoom, never the browser's actual container dimensions at save
# time.
# Tuned to a typical desktop map panel; see module docstring re: this being an approximation.
ASSUMED_VIEWPORT_WIDTH_PX = 1000
ASSUMED_VIEWPORT_HEIGHT_PX = 700

#: Bearing tolerance (degrees either side) for "an arrow points toward a pin."
ARROW_BEARING_TOLERANCE_DEGREES = 35.0

#: Below this tail-to-target separation (in degrees) the tail-to-target bearing is numerically
#: meaningless, so "does the arrow point at it" has no answer.
_DEGENERATE_TAIL_SEPARATION_DEGREES = 1e-7

#: When zoomed out, how far beyond the viewport bounds a candidate pin may
#: still be considered (an arrow drawn near the edge of the frame may point at
#: a pin just outside it) - a query-cost prefilter, not a correctness rule.
CANDIDATE_RADIUS_MULTIPLIER = 5

#: How much of a zoomed-in frame counts as "what this map is aimed at", as a fraction of its width
#: and height about the centre.
#: Whole-frame containment was the old rule and it is far too coarse to mean anything: at the default
#: threshold zoom the frame spans roughly 9 x 7 km, so sending a map of one building
AIMED_AT_VIEWPORT_FRACTION = 0.25

#: The boundary type used for pin-share detection matching.
_DETECTION_BOUNDARY_TYPE = "property"

#: A circle wider than this marks an area rather than a place, so its centre is not one the map shares.
MAX_PLACE_CIRCLE_METERS = 500


@dataclass(frozen=True)
class MapBounds:
    """A lat/lng bounding box."""

    south: float
    west: float
    north: float
    east: float

    def contains_point(self, lat: float, lng: float) -> bool:
        """Whether ``(lat, lng)`` falls within this box.

        Args:
            lat: Latitude to test.
            lng: Longitude to test.

        Returns:
            True if the point is inside (inclusive of the edges).
        """
        return self.south <= lat <= self.north and self.west <= lng <= self.east

    def expanded(self, factor: float) -> MapBounds:
        """Return a copy of this box scaled about its own center.

        Args:
            factor: Scale factor (e.g. 5 returns a box 5x as wide/tall).

        Returns:
            The expanded box.
        """
        lat_pad = (self.north - self.south) * (factor - 1) / 2
        lng_pad = (self.east - self.west) * (factor - 1) / 2
        return MapBounds(self.south - lat_pad, self.west - lng_pad, self.north + lat_pad, self.east + lng_pad)


def is_zoomed_in(zoom: float | None, *, threshold: float | None = None) -> bool:
    """Whether a saved viewport counts as "zoomed in" for detection purposes.

    Args:
        zoom: ``MarkupMap.zoom`` (Leaflet zoom level; higher = more zoomed in, a smaller geographic area visible).
        threshold: Override for testing; defaults to ``settings.UL_MAP_SHARE_ZOOM_THRESHOLD``.

    Returns:
        True when ``zoom >= threshold``."""
    if zoom is None:
        return False
    effective = threshold if threshold is not None else settings.UL_MAP_SHARE_ZOOM_THRESHOLD
    return zoom >= effective


def viewport_bounds(center_lat: float, center_lng: float, zoom: float, bearing: float = 0.0) -> MapBounds:
    """Approximate the visible lat/lng bounds for a saved MarkupMap viewport.
    This is necessarily approximate: the real visible bounds depend on the client's actual container pixel size at save time, which ``MarkupMap`` never persists (only the centre, zoom and bearing are stored).

    Args:
        center_lat: Saved viewport center latitude.
        center_lng: Saved viewport center longitude.
        zoom: Saved viewport zoom level.
        bearing: Saved rotation in degrees. A turned frame shows corners an upright one does not, so the box also covers
            the one around the turned frame. It is never narrower than the upright box: the frame's size is a guess
            either way, and a box that misses a corner misses a pin shown there.

    Returns:
        The approximated visible bounds."""
    meters_per_px = meters_per_pixel(center_lat, zoom)
    turn = math.radians(degrees_or_none(bearing) or 0.0)
    cos_t, sin_t = abs(math.cos(turn)), abs(math.sin(turn))
    width_px = max(ASSUMED_VIEWPORT_WIDTH_PX, ASSUMED_VIEWPORT_WIDTH_PX * cos_t + ASSUMED_VIEWPORT_HEIGHT_PX * sin_t)
    height_px = max(ASSUMED_VIEWPORT_HEIGHT_PX, ASSUMED_VIEWPORT_WIDTH_PX * sin_t + ASSUMED_VIEWPORT_HEIGHT_PX * cos_t)
    half_width_m = (width_px / 2) * meters_per_px
    half_height_m = (height_px / 2) * meters_per_px
    dlat = half_height_m / _METERS_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(center_lat)), 1e-6)
    dlng = half_width_m / (_METERS_PER_DEGREE_LNG_AT_EQUATOR * cos_lat)
    return MapBounds(south=center_lat - dlat, west=center_lng - dlng, north=center_lat + dlat, east=center_lng + dlng)


def geometry_to_geos(geometry: dict | None) -> GEOSGeometry | None:
    """Convert a ``PinMarkup.geometry`` dict to a GEOS geometry (SRID 4326).

    Args:
        geometry: The raw geometry dict from a PinMarkup row.

    Returns:
        A GEOS geometry, or None if the geometry is missing/malformed."""
    if not geometry:
        return None
    geom_type = geometry.get("type")
    if geom_type == "Circle":
        coords = geometry.get("coordinates")
        radius = geometry.get("radius")
        if not coords or len(coords) != 2 or radius is None:
            return None
        try:
            lng, lat = float(coords[0]), float(coords[1])
            radius_meters = float(radius)
        except (TypeError, ValueError):
            return None
        from urbanlens.dashboard.models.boundary.queryset import buffer_point_by_meters

        return buffer_point_by_meters(Point(lng, lat, srid=4326), radius_meters, latitude=lat)
    try:
        geom = GEOSGeometry(json.dumps(geometry))
    except Exception:
        logger.debug("Could not convert markup geometry to GEOS: type=%s", geom_type)
        return None
    if geom.srid is None:
        geom.srid = 4326
    return geom


def bearing_degrees(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> float:
    """Return the forward azimuth (great-circle bearing) from one point to another.

    Args:
        from_lat: Origin latitude.
        from_lng: Origin longitude.
        to_lat: Destination latitude.
        to_lng: Destination longitude.

    Returns:
        Bearing in degrees, ``0 <= bearing < 360``, where 0 is north and 90 is east."""
    phi1 = math.radians(from_lat)
    phi2 = math.radians(to_lat)
    dlambda = math.radians(to_lng - from_lng)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(x, y)) % 360


def arrow_points_toward(item: PinMarkup, target: Point, *, tolerance_degrees: float = ARROW_BEARING_TOLERANCE_DEGREES) -> bool:
    """Whether an arrow/line markup item's direction points toward a target point.

    Args:
        item: A ``line`` or ``arrow`` PinMarkup item.
        target: The point being tested (typically a pin boundary's centroid).
        tolerance_degrees: Allowed deviation either side of the exact bearing.

    Returns:
        True if the tail-to-head bearing is within tolerance of the tail-to-target bearing."""
    coords = (item.geometry or {}).get("coordinates") or []
    if len(coords) < 2:
        return False
    try:
        tail_lng, tail_lat = float(coords[0][0]), float(coords[0][1])
        head_lng, head_lat = float(coords[-1][0]), float(coords[-1][1])
    except (TypeError, ValueError, IndexError):
        return False
    if math.hypot(target.x - tail_lng, target.y - tail_lat) < _DEGENERATE_TAIL_SEPARATION_DEGREES:
        return False
    arrow_bearing = bearing_degrees(tail_lat, tail_lng, head_lat, head_lng)
    target_bearing = bearing_degrees(tail_lat, tail_lng, target.y, target.x)
    diff = abs(arrow_bearing - target_bearing) % 360
    return min(diff, 360 - diff) <= tolerance_degrees


def shape_overlaps_boundary(item: PinMarkup, boundary: GEOSGeometry) -> bool:
    """Whether a shape/marker markup item's geometry intersects a pin's boundary.

    Args:
        item: A ``square``/``circle``/``polygon``/``pin``/``text`` PinMarkup item.
        boundary: The candidate pin's effective boundary polygon.

    Returns:
        True if the item's geometry intersects the boundary.
    """
    geom = geometry_to_geos(item.geometry)
    return geom is not None and geom.intersects(boundary)


def _item_matches_pin(item: PinMarkup, boundary: GEOSGeometry) -> bool:
    """Whether a single markup item counts as "calling out" a pin's boundary."""
    if item.markup_type in (MarkupType.ARROW, MarkupType.LINE):
        return arrow_points_toward(item, boundary.centroid)
    if item.markup_type in (MarkupType.PIN, MarkupType.TEXT, MarkupType.SQUARE, MarkupType.CIRCLE, MarkupType.POLYGON):
        return shape_overlaps_boundary(item, boundary)
    return False


def _candidate_pins(sender: Profile, bounds: MapBounds):
    """Sender's own root pins within a bounding box, prefiltered on plain numeric fields."""
    from urbanlens.dashboard.models.pin.model import Pin

    return (
        Pin.objects.root_pins()
        .filter(
            profile=sender,
            location__isnull=False,
            location__latitude__range=(bounds.south, bounds.north),
            location__longitude__range=(bounds.west, bounds.east),
        )
        .select_related("location", "location__wiki", "wiki")
    )


def _boundaries_for_pins(pins: list[Pin], boundary_type: str) -> dict[int, GEOSGeometry]:
    """Batch-resolve each pin's effective boundary polygon, keyed by pin id.
    This instead fetches each of those in one bulk query for the whole batch and resolves every pin's polygon from the resulting dicts.

    Args:
        pins: Candidate pins to resolve (already ``select_related`` for ``location``, ``location__wiki``, and ``wiki`` - see :func:`_candidate_pins`).
        boundary_type: A ``BoundaryType`` value.

    Returns:
        Dict mapping pin id to its effective boundary polygon; pins with no applicable boundary (including no fallback circle) are omitted."""
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.boundary.queryset import circle_for_coordinates
    from urbanlens.dashboard.models.wiki.model import Wiki

    if not pins:
        return {}

    own_by_pin_id = Boundary.objects.rows_by_pin_id((pin.pk for pin in pins), boundary_type)

    wiki_by_pin_id: dict[int, Wiki] = {}
    for pin in pins:
        if pin.pk in own_by_pin_id and (own_by_pin_id[pin.pk].polygon or own_by_pin_id[pin.pk].generated_polygon):
            continue
        wiki = pin.wiki if pin.wiki_id else (Wiki.objects.get_for_location(pin.location) if pin.location_id else None)
        if wiki is not None:
            wiki_by_pin_id[pin.pk] = wiki

    wiki_boundary_by_wiki_id = Boundary.objects.rows_by_wiki_id((wiki.pk for wiki in wiki_by_pin_id.values()), boundary_type)
    place_polygon_by_location_id = Boundary.objects.official_polygons_by_location_id(
        (pin.location_id for pin in pins if pin.location_id),
        boundary_type,
    )

    result: dict[int, GEOSGeometry] = {}
    for pin in pins:
        own_row = own_by_pin_id.get(pin.pk)
        if own_row is not None:
            if own_row.polygon:
                result[pin.pk] = own_row.polygon
                continue
            if own_row.generated_polygon:
                result[pin.pk] = own_row.generated_polygon
                continue

        # Scope gate, exactly as in resolve_for_pin: a placed marker that
        # declines to draw this kind of boundary must not then fall through to
        # a wiki shape or a circle standing in for one.
        placed = pin.location_id in place_polygon_by_location_id if pin.location_id else False
        scoped = place_polygon_by_location_id.get(pin.location_id) if placed else None
        if placed and scoped is None:
            continue

        wiki = wiki_by_pin_id.get(pin.pk)
        wiki_row = wiki_boundary_by_wiki_id.get(wiki.pk) if wiki is not None else None
        if wiki_row is not None and wiki_row.drawn_or_generated_polygon:
            result[pin.pk] = wiki_row.drawn_or_generated_polygon
            continue

        if scoped is not None:
            result[pin.pk] = scoped
            continue

        if pin.location_id and boundary_type == _DETECTION_BOUNDARY_TYPE:
            circle = circle_for_coordinates(pin.location.latitude, pin.location.longitude)
            if circle is not None:
                result[pin.pk] = circle
    return result


def detect_shared_pins(markup_map: MarkupMap, sender: Profile) -> list[Pin]:
    """Evaluate ``sender``'s own pins against ``markup_map`` and return matches.

    Args:
        markup_map: The map being shared.
        sender: Whose pins are evaluated - always the map's effective owner at send time, never the recipient.

    Returns:
        Distinct list of ``sender``'s Pin instances "shared" by this map, per the aimed-at/markup rules described in the module docstring."""
    if markup_map.center_latitude is None or markup_map.center_longitude is None or markup_map.zoom is None:
        return []

    bounds = viewport_bounds(markup_map.center_latitude, markup_map.center_longitude, markup_map.zoom, markup_map.bearing or 0.0)
    items = list(markup_map.items.all())

    if is_zoomed_in(markup_map.zoom):
        aimed_at = bounds.expanded(AIMED_AT_VIEWPORT_FRACTION)
        candidates = [pin for pin in _candidate_pins(sender, bounds) if bounds.contains_point(float(pin.location.latitude), float(pin.location.longitude))]
    else:
        if not items:
            return []
        aimed_at = None
        candidates = list(_candidate_pins(sender, bounds.expanded(CANDIDATE_RADIUS_MULTIPLIER)))

    boundaries_by_pin_id = _boundaries_for_pins(candidates, _DETECTION_BOUNDARY_TYPE)

    matches: list[Pin] = []
    for pin in candidates:
        if aimed_at is not None and aimed_at.contains_point(float(pin.location.latitude), float(pin.location.longitude)):
            matches.append(pin)
            continue
        boundary = boundaries_by_pin_id.get(pin.pk)
        if boundary is None:
            continue
        if any(_item_matches_pin(item, boundary) for item in items):
            matches.append(pin)
    return matches


def _marked_point(item: PinMarkup) -> tuple[float, float] | None:
    """The one place ``item`` marks by itself: a marker's or label's point, or a property-sized circle's centre."""
    geometry = item.geometry or {}
    if item.markup_type in (MarkupType.PIN, MarkupType.TEXT):
        if geometry.get("type") != "Point":
            return None
    elif item.markup_type == MarkupType.CIRCLE:
        radius = geometry.get("radius")
        if geometry.get("type") != "Circle" or radius is None:
            return None
        try:
            if float(radius) > MAX_PLACE_CIRCLE_METERS:
                return None
        except (TypeError, ValueError):
            return None
    else:
        return None
    try:
        longitude, latitude = float(geometry["coordinates"][0]), float(geometry["coordinates"][1])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0) or (latitude, longitude) == (0.0, 0.0):
        return None
    return round(latitude, 6), round(longitude, 6)


def marked_places(markup_map: MarkupMap, shared_pins: list[Pin], *, limit: int) -> list[tuple[float, float]]:
    """The places ``markup_map`` marks outside every pin it was found to share, in drawing order.

    A marker or text label marks its point and a property-sized circle its centre. A line, arrow, square or polygon has
    no one point its sender aimed at, so it shares only the pins it calls out.

    Args:
        markup_map: The map being shared.
        shared_pins: The sender's pins :func:`detect_shared_pins` matched; a point inside one is already shared.
        limit: How many places to return at most.

    Returns:
        ``(latitude, longitude)`` pairs, each once.
    """
    boundaries = list(_boundaries_for_pins(shared_pins, _DETECTION_BOUNDARY_TYPE).values())
    places: list[tuple[float, float]] = []
    for item in markup_map.items.all():
        point = _marked_point(item)
        if point is None or point in places:
            continue
        spot = Point(point[1], point[0], srid=4326)
        if any(boundary.intersects(spot) for boundary in boundaries):
            continue
        places.append(point)
        if len(places) == limit:
            break
    return places


def sync_pin_inferences(markup_map: MarkupMap) -> list[Pin]:
    """Recompute ``markup_map.inferred_pins`` from its current geometry.

    Args:
        markup_map: The map to (re)sync.

    Returns:
        The freshly-detected list of pins (so callers like :func:`~urbanlens.dashboard.services.sharing.map_sharing.share_markup_map_with_profile` that need the current set don't have to run detection twice)."""
    pins = detect_shared_pins(markup_map, markup_map.profile)
    markup_map.inferred_pins.set(pins)
    return pins
