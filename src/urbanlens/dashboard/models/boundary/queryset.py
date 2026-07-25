"""Boundary queryset and manager."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Self

from django.contrib.gis.geos import Point, Polygon

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Radius (metres) of the synthesized circle fallback when a location has no
#: property boundary at all.
DEFAULT_RADIUS_METERS = 50

#: Metres per degree of latitude (WGS-84), effectively constant regardless of
#: latitude - unlike a degree of longitude, whose real-world length shrinks by
#: a factor of cos(latitude) away from the equator. Shared by every
#: metres<->degrees conversion in this codebase that needs the correction
#: (see :func:`meters_to_lng_degrees` / :func:`buffer_point_by_meters`).
METERS_PER_DEGREE_LAT = 111_320.0


def meters_to_lng_degrees(meters: float, latitude: float) -> float:
    """Convert a real-world east-west distance to a longitude-degree delta.

    A degree of longitude spans fewer real-world metres away from the
    equator (by a factor of ``cos(latitude)``), so naively dividing by a
    constant metres-per-degree value (as if longitude behaved like latitude)
    understates the delta needed at higher latitudes. This is the one
    implementation every metres->degrees-of-longitude conversion in the
    codebase should share, instead of re-deriving the ``cos(latitude)``
    correction locally.

    Args:
        meters: Real-world east-west distance, in metres.
        latitude: Latitude (degrees) at which to evaluate the conversion.

    Returns:
        The longitude delta, in degrees.
    """
    return meters / (METERS_PER_DEGREE_LAT * max(math.cos(math.radians(latitude)), 1e-6))


def buffer_point_by_meters(point: Point, radius_meters: float, *, latitude: float | None = None) -> Polygon:
    """Buffer a WGS-84 point by a real-world metre radius, as a true circle.

    ``GEOSGeometry.buffer()`` operates in the geometry's own coordinate units
    (degrees, for SRID 4326), so naively converting ``radius_meters`` to
    degrees with a single constant and buffering by that amount on both axes
    produces a shape that is a circle in *degree* space but an ellipse in
    *real-world distance*: it is squashed east-west, since a degree of
    longitude covers fewer metres than a degree of latitude away from the
    equator. This buffers in degree space using the latitude-only
    conversion (giving the correct north-south extent), then stretches the
    longitude axis of the result by ``1 / cos(latitude)`` around the centre
    point so the real-world east-west extent matches the requested radius
    too.

    Args:
        point: The WGS-84 (SRID 4326) centre point.
        radius_meters: Desired real-world radius, in metres.
        latitude: Latitude to use for the ``cos(latitude)`` correction;
            defaults to the point's own latitude (``point.y``).

    Returns:
        A Polygon (SRID 4326) approximating a real-world circle of the given
        radius around ``point``.
    """
    lat = latitude if latitude is not None else point.y
    lat_deg = radius_meters / METERS_PER_DEGREE_LAT
    circle = point.buffer(lat_deg)
    lng_scale = meters_to_lng_degrees(radius_meters, lat) / lat_deg if lat_deg else 1.0
    ring = circle.exterior_ring
    scaled_coords = [(point.x + (x - point.x) * lng_scale, y) for x, y in ring.coords]
    return Polygon(scaled_coords, srid=point.srid or 4326)


def circle_for_coordinates(latitude, longitude, radius_meters: int = DEFAULT_RADIUS_METERS) -> GEOSGeometry | None:
    """Return the default circular boundary around a coordinate pair.

    Args:
        latitude: WGS-84 latitude, or None.
        longitude: WGS-84 longitude, or None.
        radius_meters: Circle radius in metres.

    Returns:
        A polygonal buffer around the point, or None when coordinates are missing.
    """
    if latitude is None or longitude is None:
        return None
    center = Point(float(longitude), float(latitude), srid=4326)
    return buffer_point_by_meters(center, radius_meters, latitude=float(latitude))


class BoundaryQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Boundary - typed spatial regions for Locations, Wikis, and Pins."""

    def of_type(self, boundary_type: str) -> Self:
        """Boundaries of one type (property or building)."""
        return self.filter(boundary_type=boundary_type)

    def location_defaults(self) -> Self:
        """Location-default boundaries (no pin, no wiki, no profile, no source).

        These are the shared, API-generated rows used for point matching.
        Per-provider source-candidate rows (``source`` set) are excluded: they
        exist only for boundary voting, and the winning candidate's geometry
        is materialized onto these canonical rows instead.
        """
        return self.filter(pin__isnull=True, wiki__isnull=True, profile__isnull=True, location__isnull=False, source="")

    def source_candidates_for_location(self, location) -> Self:
        """Per-provider candidate boundaries for a location (see boundary voting)."""
        return self.filter(pin__isnull=True, wiki__isnull=True, profile__isnull=True, location=location).exclude(source="")

    def for_profile(self, profile) -> Self:
        """Pin-scoped boundaries belonging to a given profile."""
        return self.filter(profile=profile, pin__isnull=False)

    def for_wiki(self, wiki) -> Self:
        """Wiki-customized boundaries for a given wiki."""
        return self.filter(wiki=wiki, pin__isnull=True)

    def for_location(self, location) -> Self:
        """Location-default boundaries for a given location."""
        return self.location_defaults().filter(location=location)

    def for_pin(self, pin) -> Self:
        """Pin-scoped boundaries for a specific pin."""
        return self.filter(pin=pin)

    def with_coordinate_location(self) -> Self:
        """Prefetch location/wiki/pin so effective_polygon avoids extra queries."""
        return self.select_related("location", "wiki__location", "pin__location")


class BoundaryManager(abstract.DashboardManager.from_queryset(BoundaryQuerySet)):
    """Manager for Boundary.

    Resolution helpers return *polygons* (not rows) because the effective
    boundary for a pin or wiki may be synthesized (circle fallback) or
    inherited from a parent pin, neither of which maps to a stored row.
    """

    def get_or_create_location_default(self, location: Location, boundary_type: str, defaults: dict[str, Any] | None = None):
        """Get or create the shared location-default boundary row of one type.

        Args:
            location: The Location the boundary describes.
            boundary_type: A :class:`BoundaryType` value.
            defaults: Optional field overrides for row creation.

        Returns:
            Tuple of (Boundary, created).
        """
        return self.get_or_create(
            location=location,
            boundary_type=boundary_type,
            pin=None,
            wiki=None,
            profile=None,
            source="",
            defaults=dict(defaults or {}),
        )

    def row_for_wiki(self, wiki: Wiki, boundary_type: str):
        """The wiki-customized boundary row of one type, or None."""
        return self.for_wiki(wiki).of_type(boundary_type).with_coordinate_location().first()

    def row_for_pin(self, pin: Pin, boundary_type: str):
        """The pin's own boundary row of one type, or None."""
        return self.filter(pin=pin, boundary_type=boundary_type).with_coordinate_location().first()

    def row_for_location(self, location: Location, boundary_type: str):
        """The location-default boundary row of one type, or None."""
        return self.for_location(location).of_type(boundary_type).with_coordinate_location().first()

    # ------------------------------------------------------------------
    # Batched row lookups
    #
    # Counterparts to row_for_pin/row_for_wiki/row_for_location for callers
    # resolving many pins' boundaries at once (e.g. markup-triggered pin-share
    # detection) - one query for the whole batch instead of one per item.
    # ------------------------------------------------------------------

    def rows_by_pin_id(self, pin_ids: Iterable[int], boundary_type: str) -> dict[int, Boundary]:
        """Bulk-fetch pin-owned boundary rows of one type, keyed by pin id.

        Args:
            pin_ids: Primary keys of the pins to look up.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Dict mapping pin id to its Boundary row, for pins that have one.
        """
        pin_ids = list(pin_ids)
        if not pin_ids:
            return {}
        return {row.pin_id: row for row in self.filter(pin_id__in=pin_ids, boundary_type=boundary_type)}

    def rows_by_wiki_id(self, wiki_ids: Iterable[int], boundary_type: str) -> dict[int, Boundary]:
        """Bulk-fetch wiki-owned boundary rows of one type, keyed by wiki id.

        Args:
            wiki_ids: Primary keys of the wikis to look up.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Dict mapping wiki id to its Boundary row, for wikis that have one.
        """
        wiki_ids = list(wiki_ids)
        if not wiki_ids:
            return {}
        return {row.wiki_id: row for row in self.filter(wiki_id__in=wiki_ids, boundary_type=boundary_type)}

    def rows_by_location_id(self, location_ids: Iterable[int], boundary_type: str) -> dict[int, Boundary]:
        """Bulk-fetch location-default boundary rows of one type, keyed by location id.

        Args:
            location_ids: Primary keys of the locations to look up.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Dict mapping location id to its default Boundary row, for
            locations that have one.
        """
        location_ids = list(location_ids)
        if not location_ids:
            return {}
        return {row.location_id: row for row in self.location_defaults().filter(location_id__in=location_ids, boundary_type=boundary_type)}

    # ------------------------------------------------------------------
    # Effective-polygon resolution
    # ------------------------------------------------------------------

    def resolve_for_wiki(self, wiki: Wiki, boundary_type: str) -> tuple[GEOSGeometry | None, str | None]:
        """Resolve the polygon to display for a wiki page, with its source.

        Order: wiki-customized row → location-default generated polygon →
        circle fallback (property only; buildings have no fallback shape).

        Args:
            wiki: The Wiki whose boundary is being displayed.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Tuple of (polygon, source) where source is one of "wiki",
            "generated", "circle", or (None, None) when nothing applies.
        """
        from urbanlens.dashboard.models.boundary.model import BoundaryType

        if (row := self.row_for_wiki(wiki, boundary_type)) and row.drawn_or_generated_polygon:
            return row.drawn_or_generated_polygon, "wiki"
        if wiki.location_id:
            if (row := self.row_for_location(wiki.location, boundary_type)) and row.generated_polygon:
                return row.generated_polygon, "generated"
            if boundary_type == BoundaryType.PROPERTY:
                circle = circle_for_coordinates(wiki.location.latitude, wiki.location.longitude)
                if circle is not None:
                    return circle, "circle"
        return None, None

    def resolve_for_pin(self, pin: Pin, boundary_type: str) -> tuple[GEOSGeometry | None, str | None]:
        """Resolve the polygon that applies to a pin, with its source.

        Property order: pin's own row → parent pin's effective property, but
        only when this pin sits inside it (a detail pin placed outside its
        parent's property must not inherit that property's boundary) →
        wiki-customized row → location-default generated polygon → circle
        fallback around the location's coordinates.

        Building order: pin's own row → parent pin's effective building, but
        only when this pin sits inside it (detail pins for other buildings on
        the property must not inherit the main building) → for root pins,
        wiki-customized row → location-default generated polygon. No circle
        fallback: a missing building boundary means "no known building".

        Args:
            pin: The Pin to resolve a boundary for.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Tuple of (polygon, source) where source is one of "pin",
            "inherited", "wiki", "generated", "circle", or (None, None) when
            nothing applies.
        """
        from urbanlens.dashboard.models.boundary.model import BoundaryType

        if row := self.row_for_pin(pin, boundary_type):
            if row.polygon:
                return row.polygon, "pin"
            if row.generated_polygon:
                return row.generated_polygon, "generated"

        if pin.parent_pin_id and (parent_pin := pin.parent_pin) is not None:
            parent_polygon, _parent_source = self.resolve_for_pin(parent_pin, boundary_type)
            if parent_polygon is not None:
                point = self._pin_point(pin)
                if point is not None and (parent_polygon.contains(point) or parent_polygon.touches(point)):
                    return parent_polygon, "inherited"
            if boundary_type != BoundaryType.PROPERTY:
                # Buildings: a detail pin outside the parent's building has no
                # building of its own - no further fallback.
                return None, None
            # Property: a detail pin outside the parent's property boundary
            # (or whose parent has none) falls through to its own
            # wiki/location/circle chain below, using its own Location.

        # Prefer the pin's explicitly chosen wiki; fall back to the location's
        # wiki for pins that were never explicitly linked (e.g. bulk imports).
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki = pin.wiki if pin.wiki_id else (Wiki.objects.get_for_location(pin.location) if pin.location_id else None)
        if wiki is not None and (row := self.row_for_wiki(wiki, boundary_type)) and row.drawn_or_generated_polygon:
            return row.drawn_or_generated_polygon, "wiki"
        if pin.location_id:
            if (row := self.row_for_location(pin.location, boundary_type)) and row.generated_polygon:
                return row.generated_polygon, "generated"
            if boundary_type == BoundaryType.PROPERTY:
                circle = circle_for_coordinates(pin.location.latitude, pin.location.longitude)
                if circle is not None:
                    return circle, "circle"
        return None, None

    def effective_polygon_for_wiki(self, wiki: Wiki, boundary_type: str) -> GEOSGeometry | None:
        """The polygon to display for a wiki page (see ``resolve_for_wiki``)."""
        return self.resolve_for_wiki(wiki, boundary_type)[0]

    def effective_polygon_for_pin(self, pin: Pin, boundary_type: str) -> GEOSGeometry | None:
        """The polygon that applies to a pin (see ``resolve_for_pin``)."""
        return self.resolve_for_pin(pin, boundary_type)[0]

    @staticmethod
    def _pin_point(pin: Pin) -> Point | None:
        """The pin's marker coordinates as a GEOS point, or None."""
        lat = pin.effective_latitude
        lon = pin.effective_longitude
        if lat is None or lon is None:
            return None
        return Point(float(lon), float(lat), srid=4326)
