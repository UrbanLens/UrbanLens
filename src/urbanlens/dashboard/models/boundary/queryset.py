"""Boundary queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from django.contrib.gis.geos import Point

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Radius (metres) of the synthesized circle fallback when a location has no
#: property boundary at all.
DEFAULT_RADIUS_METERS = 50


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
    radius_deg = radius_meters / 111_000
    return center.buffer(radius_deg)


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
