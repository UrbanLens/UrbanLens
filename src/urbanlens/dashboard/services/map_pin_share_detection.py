"""Geometry-based detection of which pins a MarkupMap effectively shares.

When one profile sends a MarkupMap to another (a DM attachment, a standalone
map share, or a map attached to an explicit pin share), the map's viewport and
markup may reveal the location of one or more of the sender's own pins even
though the sender never used the explicit share-a-pin dialog. This module
answers "which of the sender's pins does this map reveal?" so the caller can
record those as :class:`~urbanlens.dashboard.models.pin_share.model.PinShare`
rows via ``services.map_sharing``.

Two detection modes, chosen by the saved viewport's zoom level relative to
``settings.UL_MAP_SHARE_ZOOM_THRESHOLD``:

- Zoomed in (a small geographic area is visible): the map is clearly pointing
  at a specific place, so every one of the sender's pins visible in frame
  counts as shared, regardless of what markup exists.
- Zoomed out (a large geographic area is visible): the sender could be
  sharing the map for many reasons unrelated to any one pin, so only pins
  specifically called out by markup count - a pin/text marker sitting in the
  pin's boundary, an arrow/line pointing toward it, or a shape overlapping it.

``PinMarkup.geometry`` is a plain JSONField (not a PostGIS field), so this
module bridges it to GEOS geometries itself; ``MarkupMap`` only ever persists
a saved center+zoom, never the client's actual viewport pixel size, so
``viewport_bounds`` is a best-effort approximation rather than an exact
reproduction of what the sender saw on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point

from urbanlens.dashboard.models.markup.meta import MarkupType

if TYPE_CHECKING:
    from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# Metres-per-pixel at zoom 0 on the equator - standard Web Mercator constant.
_EARTH_CIRCUMFERENCE_M = 156_543.03392
_METERS_PER_DEGREE_LAT = 111_320.0
_METERS_PER_DEGREE_LNG_AT_EQUATOR = 111_320.0

# Assumed client viewport size in CSS pixels, standing in for the real
# (unknown) container size - MarkupMap only ever persists center+zoom, never
# the browser's actual container dimensions at save time. Tuned to a typical
# desktop map panel; see module docstring re: this being an approximation.
ASSUMED_VIEWPORT_WIDTH_PX = 1000
ASSUMED_VIEWPORT_HEIGHT_PX = 700

#: Bearing tolerance (degrees either side) for "an arrow points toward a pin."
ARROW_BEARING_TOLERANCE_DEGREES = 35.0

#: When zoomed out, how far beyond the viewport bounds a candidate pin may
#: still be considered (an arrow drawn near the edge of the frame may point at
#: a pin just outside it) - a query-cost prefilter, not a correctness rule.
CANDIDATE_RADIUS_MULTIPLIER = 5

#: The boundary type used for pin-share detection matching.
_DETECTION_BOUNDARY_TYPE = "property"


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
        zoom: ``MarkupMap.zoom`` (Leaflet zoom level; higher = more zoomed in,
            a smaller geographic area visible). None (never saved) is treated
            as not zoomed in.
        threshold: Override for testing; defaults to
            ``settings.UL_MAP_SHARE_ZOOM_THRESHOLD``.

    Returns:
        True when ``zoom >= threshold``.
    """
    if zoom is None:
        return False
    effective = threshold if threshold is not None else settings.UL_MAP_SHARE_ZOOM_THRESHOLD
    return zoom >= effective


def viewport_bounds(center_lat: float, center_lng: float, zoom: float) -> MapBounds:
    """Approximate the visible lat/lng bounds for a saved MarkupMap viewport.

    This is necessarily approximate: the real visible bounds depend on the
    client's actual container pixel size at save time, which ``MarkupMap``
    never persists (only ``center_latitude``/``center_longitude``/``zoom``
    are stored). Uses standard Web Mercator meters-per-pixel math against an
    assumed viewport size.

    Args:
        center_lat: Saved viewport center latitude.
        center_lng: Saved viewport center longitude.
        zoom: Saved viewport zoom level.

    Returns:
        The approximated visible bounds.
    """
    meters_per_px = _EARTH_CIRCUMFERENCE_M * math.cos(math.radians(center_lat)) / (2**zoom)
    half_width_m = (ASSUMED_VIEWPORT_WIDTH_PX / 2) * meters_per_px
    half_height_m = (ASSUMED_VIEWPORT_HEIGHT_PX / 2) * meters_per_px
    dlat = half_height_m / _METERS_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(center_lat)), 1e-6)
    dlng = half_width_m / (_METERS_PER_DEGREE_LNG_AT_EQUATOR * cos_lat)
    return MapBounds(south=center_lat - dlat, west=center_lng - dlng, north=center_lat + dlat, east=center_lng + dlng)


def geometry_to_geos(geometry: dict | None) -> GEOSGeometry | None:
    """Convert a ``PinMarkup.geometry`` dict to a GEOS geometry (SRID 4326).

    Standard GeoJSON shapes (``Point``/``LineString``/``Polygon`` - covering
    the ``line``/``arrow``/``text``/``square``/``polygon``/``pin`` markup
    types) convert directly. The non-standard ``Circle`` type (
    ``{"type": "Circle", "coordinates": [lng, lat], "radius": meters}``) has
    no GeoJSON equivalent and is synthesized as a buffered point, using the
    same degrees-per-metre approximation as
    ``models.boundary.queryset.circle_for_coordinates``.

    Args:
        geometry: The raw geometry dict from a PinMarkup row.

    Returns:
        A GEOS geometry, or None if the geometry is missing/malformed.
    """
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
            radius_deg = float(radius) / 111_000
        except (TypeError, ValueError):
            return None
        return Point(lng, lat, srid=4326).buffer(radius_deg)
    try:
        geom = GEOSGeometry(json.dumps(geometry))
    except Exception:
        logger.debug("Could not convert markup geometry to GEOS: %r", geometry)
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
        Bearing in degrees, ``0 <= bearing < 360``, where 0 is north and 90 is east.
    """
    phi1 = math.radians(from_lat)
    phi2 = math.radians(to_lat)
    dlambda = math.radians(to_lng - from_lng)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(x, y)) % 360


def arrow_points_toward(item: PinMarkup, target: Point, *, tolerance_degrees: float = ARROW_BEARING_TOLERANCE_DEGREES) -> bool:
    """Whether an arrow/line markup item's direction points toward a target point.

    Direction is inferred from coordinate ordering (no explicit direction
    field exists on ``PinMarkup`` - the first LineString coordinate is the
    tail, the last is the head/arrowhead, per the renderer convention).

    Args:
        item: A ``line`` or ``arrow`` PinMarkup item.
        target: The point being tested (typically a pin boundary's centroid).
        tolerance_degrees: Allowed deviation either side of the exact bearing.

    Returns:
        True if the tail-to-head bearing is within tolerance of the
        tail-to-target bearing.
    """
    coords = (item.geometry or {}).get("coordinates") or []
    if len(coords) < 2:
        return False
    try:
        tail_lng, tail_lat = float(coords[0][0]), float(coords[0][1])
        head_lng, head_lat = float(coords[-1][0]), float(coords[-1][1])
    except (TypeError, ValueError, IndexError):
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

    return Pin.objects.filter(
        profile=sender,
        parent_pin__isnull=True,
        location__isnull=False,
        location__latitude__range=(bounds.south, bounds.north),
        location__longitude__range=(bounds.west, bounds.east),
    ).select_related("location")


def detect_shared_pins(markup_map: MarkupMap, sender: Profile) -> list[Pin]:
    """Evaluate ``sender``'s own pins against ``markup_map`` and return matches.

    Args:
        markup_map: The map being shared. Callers are responsible for
            confirming this is (or was) ``sender``'s own map.
        sender: Whose pins are evaluated - always the map's effective owner
            at send time, never the recipient.

    Returns:
        Distinct list of ``sender``'s Pin instances "shared" by this map, per
        the zoomed-in/zoomed-out rules described in the module docstring.
        Empty if the map has no saved viewport.
    """
    if markup_map.center_latitude is None or markup_map.center_longitude is None or markup_map.zoom is None:
        return []

    bounds = viewport_bounds(markup_map.center_latitude, markup_map.center_longitude, markup_map.zoom)

    if is_zoomed_in(markup_map.zoom):
        return [pin for pin in _candidate_pins(sender, bounds) if bounds.contains_point(float(pin.location.latitude), float(pin.location.longitude))]

    items = list(markup_map.items.all())
    if not items:
        return []

    from urbanlens.dashboard.models.boundary.model import Boundary

    wide_bounds = bounds.expanded(CANDIDATE_RADIUS_MULTIPLIER)
    matches: list[Pin] = []
    for pin in _candidate_pins(sender, wide_bounds):
        boundary = Boundary.objects.effective_polygon_for_pin(pin, _DETECTION_BOUNDARY_TYPE)
        if boundary is None:
            continue
        if any(_item_matches_pin(item, boundary) for item in items):
            matches.append(pin)
    return matches


def sync_pin_inferences(markup_map: MarkupMap) -> list[Pin]:
    """Recompute ``markup_map.inferred_pins`` from its current geometry.

    Runs :func:`detect_shared_pins` against the map's own owner and persists
    the result via a plain M2M ``.set()`` (which diffs and commits the add/
    remove itself), so ``MarkupMap.inferred_pins`` / ``Pin.inferred_maps``
    stay a durable record of geometric detection for search and pin-share
    tracking - independent of whether the map is ever actually shared, and of
    the separate, user-editable ``MarkupMap.pin`` link. See
    ``models.markup.signals`` for the save/delete hooks that call this.

    Args:
        markup_map: The map to (re)sync.

    Returns:
        The freshly-detected list of pins (so callers like
        :func:`~urbanlens.dashboard.services.map_sharing.share_markup_map_with_profile`
        that need the current set don't have to run detection twice).
    """
    pins = detect_shared_pins(markup_map, markup_map.profile)
    markup_map.inferred_pins.set(pins)
    return pins
