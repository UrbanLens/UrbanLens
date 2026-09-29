"""Boundary queryset and manager."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point, Polygon
from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Radius (metres) of the synthesized circle fallback when a location has no
#: property boundary at all.
DEFAULT_RADIUS_METERS = 50

#: Metres per degree of latitude (WGS-84), effectively constant regardless of latitude - unlike a
#: degree of longitude, whose real-world length shrinks by a factor of cos(latitude) away from the
#: equator.
#: Shared by every metres<->degrees conversion in this codebase that needs the correction (see
METERS_PER_DEGREE_LAT = 111_320.0


def meters_to_lng_degrees(meters: float, latitude: float) -> float:
    """Convert a real-world east-west distance to a longitude-degree delta.
    A degree of longitude spans fewer real-world metres away from the equator (by a factor of ``cos(latitude)``), so naively dividing by a constant metres-per-degree value (as if longitude behaved like latitude) understates the delta needed at higher latitudes.

    Args:
        meters: Real-world east-west distance, in metres.
        latitude: Latitude (degrees) at which to evaluate the conversion.

    Returns:
        The longitude delta, in degrees.
    """
    return meters / (METERS_PER_DEGREE_LAT * max(math.cos(math.radians(latitude)), 1e-6))


def buffer_point_by_meters(point: Point, radius_meters: float, *, latitude: float | None = None) -> Polygon:
    """Buffer a WGS-84 point by a real-world metre radius, as a true circle.
    ``GEOSGeometry.buffer()`` operates in the geometry's own coordinate units (degrees, for SRID 4326), so naively converting ``radius_meters`` to degrees with a single constant and buffering by that amount on both axes produces a shape that is a circle in *degree* space but an ellipse in *real-world distance*: it is squashed east-west, since a degree of longitude covers fewer metres than a degree of latitude away from the equator.

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
    if not isinstance(circle, Polygon):
        raise TypeError(f"Buffering a point produced a {type(circle).__name__}, not a Polygon.")
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

    def source_candidates_for_place(self, place) -> Self:
        """Per-provider candidate boundaries for a place (see boundary voting)."""
        return self.filter(pin__isnull=True, wiki__isnull=True, profile__isnull=True, place=place).exclude(source="")

    def for_wiki(self, wiki) -> Self:
        """Wiki-customized boundaries for a given wiki."""
        return self.filter(wiki=wiki, pin__isnull=True)

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

    def row_for_wiki(self, wiki: Wiki, boundary_type: str):
        """The wiki-customized boundary row of one type, or None."""
        return self.for_wiki(wiki).of_type(boundary_type).with_coordinate_location().first()

    def row_for_pin(self, pin: Pin, boundary_type: str):
        """The pin's own boundary row of one type, or None."""
        return self.for_pin(pin).of_type(boundary_type).with_coordinate_location().first()

    # ------------------------------------------------------------------
    # Batched row lookups Counterparts to row_for_pin/row_for_wiki/row_for_location for callers
    # resolving many pins' boundaries at once (e.g. markup-triggered pin-share detection) - one
    # query for the whole batch instead of one per item.
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

    def official_polygons_by_location_id(self, location_ids: Iterable[int], boundary_type: str) -> dict[int, GEOSGeometry | None]:
        """Bulk-resolve each location's official place outline, keyed by location id.
        The batched counterpart to the place step of :meth:`resolve_for_pin`, for callers resolving many markers at once.

        Args:
            location_ids: Primary keys of the locations to look up.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Dict mapping location id to a polygon or None, for placed
            locations only.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.places.scope import place_polygon

        location_ids = list(location_ids)
        if not location_ids:
            return {}
        rows = Location.objects.filter(pk__in=location_ids, place__isnull=False).select_related("place", "place__parent")
        return {location.pk: place_polygon(location.place, boundary_type) for location in rows}

    # ------------------------------------------------------------------
    # Effective-polygon resolution
    # ------------------------------------------------------------------

    def resolve_for_wiki(self, wiki: Wiki, boundary_type: str) -> tuple[GEOSGeometry | None, str | None]:
        """Resolve the polygon to display for a wiki page, with its source.
        Scope first (a wiki anchored to a building draws the footprint, not the parcel), then wiki-customized row → the wiki's place outline → circle fallback (property only; buildings have no fallback shape).
        Scoping here is what makes "the boundary used when creating a wiki for a building is the building's" true by construction rather than by every call site remembering to ask for the right type.

        Args:
            wiki: The Wiki whose boundary is being displayed. When this is a
                concealed projection (``services.wiki.concealment.is_concealed``),
                a wiki-drawn customization is skipped in favour of the place/
                circle fallback - a wiki-scoped ``Boundary`` row records no
                author at all (always ``profile=None``), so unlike markup or
                custom layers it cannot get the own-contribution treatment;
                hiding it outright is the only option that doesn't risk
                showing a stranger's edit back as automatic.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Tuple of (polygon, source) where source is one of "wiki",
            "place", "circle", or (None, None) when nothing applies.
        """
        from urbanlens.dashboard.models.boundary.model import BoundaryType
        from urbanlens.dashboard.services.places.scope import place_polygon
        from urbanlens.dashboard.services.wiki.concealment import is_concealed

        # A wiki with no place anchor of its own still displays at a location,
        # and that location knows what it stands on - so fall back to it rather
        # than dropping to a circle.
        place = wiki.place if wiki.place_id else (wiki.location.place if (wiki.location_id and wiki.location is not None and wiki.location.place_id) else None)
        scoped = place_polygon(place, boundary_type) if place is not None else None
        if place is not None and scoped is None:
            return None, None

        if not is_concealed(wiki) and (row := self.row_for_wiki(wiki, boundary_type)) and row.drawn_or_generated_polygon:
            return row.drawn_or_generated_polygon, "wiki"
        if scoped is not None:
            return scoped, "place"
        if wiki.location_id and boundary_type == BoundaryType.PROPERTY:
            circle = circle_for_coordinates(wiki.location.latitude, wiki.location.longitude)
            if circle is not None:
                return circle, "circle"
        return None, None

    def resolve_for_pin(self, pin: Pin, boundary_type: str) -> tuple[GEOSGeometry | None, str | None]:
        """Resolve the polygon that applies to a pin, with its source.
        Two questions, in that order. **Should this marker draw this kind of boundary at all?** A marker standing on a footprint on a multi-building property is not about the 200-acre parcel under it, so a property request answers nothing - see ``services.places.scope.place_polygon``.

        Args:
            pin: The Pin to resolve a boundary for.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Tuple of (polygon, source) where source is one of "pin",
            "generated", "place", "inherited", "wiki", "circle", or
            (None, None) when nothing applies.
        """
        from urbanlens.dashboard.services.places.scope import place_polygon

        row = self.row_for_pin(pin, boundary_type)
        location = pin.location if pin.location_id else None
        place = location.place if (location is not None and location.place_id) else None
        scoped = place_polygon(place, boundary_type) if place is not None else None
        head = self._resolve_pin_head(pin, boundary_type, row=row, place=place, scoped=scoped)
        if head is not None:
            return head

        # Prefer the pin's explicitly chosen wiki; fall back to the location's
        # wiki for pins that were never explicitly linked (e.g. bulk imports).
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki = pin.wiki if pin.wiki_id else (Wiki.objects.get_for_location(location) if location is not None else None)
        wiki_row = self.row_for_wiki(wiki, boundary_type) if wiki is not None else None
        return self._resolve_pin_tail(boundary_type, location=location, scoped=scoped, wiki_row=wiki_row)

    def _resolve_pin_head(self, pin: Pin, boundary_type: str, *, row: Boundary | None, place: Place | None, scoped: GEOSGeometry | None) -> tuple[GEOSGeometry | None, str | None] | None:
        """The steps of :meth:`resolve_for_pin` that come before the wiki.

        Args:
            pin: The pin.
            boundary_type: A :class:`BoundaryType` value.
            row: The pin's own boundary row of this type, or None.
            place: The place the pin's location resolved onto, or None.
            scoped: ``place_polygon(place, boundary_type)``.

        Returns:
            The answer, or None when it depends on the wiki.
        """
        from urbanlens.dashboard.models.boundary.model import BoundaryType

        if row is not None and row.polygon:
            return row.polygon, "pin"

        # A hull fitted around this pin's own children describes the markers we happen to know
        # about, not the property - so it stands in only while nobody has offered the real outline,
        # and steps aside the moment one exists.
        # Left ahead of the place, geometry the chain had just fetched stayed invisible on the very
        if row is not None and row.generated_polygon and not (row.generated_from_children and scoped is not None):
            return row.generated_polygon, "generated"

        if place is not None and scoped is None:
            return None, None

        if scoped is None and pin.parent_pin_id and (parent_pin := pin.parent_pin) is not None:
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
            # wiki/circle chain below, using its own Location.
        return None

    @staticmethod
    def _resolve_pin_tail(boundary_type: str, *, location: Location | None, scoped: GEOSGeometry | None, wiki_row: Boundary | None) -> tuple[GEOSGeometry | None, str | None]:
        """The steps of :meth:`resolve_for_pin` from the wiki on.

        Args:
            boundary_type: A :class:`BoundaryType` value.
            location: The pin's location, or None.
            scoped: ``place_polygon(place, boundary_type)``.
            wiki_row: The boundary row of the pin's wiki, or None.

        Returns:
            The answer.
        """
        from urbanlens.dashboard.models.boundary.model import BoundaryType

        if wiki_row is not None and wiki_row.drawn_or_generated_polygon:
            return wiki_row.drawn_or_generated_polygon, "wiki"
        if scoped is not None:
            return scoped, "place"
        if location is not None and boundary_type == BoundaryType.PROPERTY:
            circle = circle_for_coordinates(location.latitude, location.longitude)
            if circle is not None:
                return circle, "circle"
        return None, None

    def effective_polygons_for_pins(self, pins: Iterable[Pin], boundary_type: str) -> dict[int, GEOSGeometry | None]:
        """:meth:`effective_polygon_for_pin` for many pins, in a fixed number of queries.

        The same chain as :meth:`resolve_for_pin`, with each step's rows fetched for the whole batch. A detail
        pin (one with a parent) can inherit its parent's boundary, which is a walk up its own ancestors, so those
        are resolved one at a time; root pins, the common case, never are.

        Args:
            pins: The pins.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            Pin id to polygon, or None where nothing applies.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.places.scope import place_polygon

        pins = list(pins)
        answers: dict[int, GEOSGeometry | None] = {pin.pk: self.effective_polygon_for_pin(pin, boundary_type) for pin in pins if pin.parent_pin_id}
        roots = [pin for pin in pins if not pin.parent_pin_id]
        if not roots:
            return answers

        rows = self.rows_by_pin_id([pin.pk for pin in roots], boundary_type)
        locations = {location.pk: location for location in Location.objects.filter(pk__in={pin.location_id for pin in roots if pin.location_id}).select_related("place", "place__parent")}
        needs_wiki: list[tuple[Pin, Location | None, GEOSGeometry | None]] = []
        for pin in roots:
            location = locations.get(pin.location_id) if pin.location_id else None
            place = location.place if (location is not None and location.place_id) else None
            scoped = place_polygon(place, boundary_type) if place is not None else None
            head = self._resolve_pin_head(pin, boundary_type, row=rows.get(pin.pk), place=place, scoped=scoped)
            if head is not None:
                answers[pin.pk] = head[0]
            else:
                needs_wiki.append((pin, location, scoped))
        if not needs_wiki:
            return answers

        wiki_of = self._wikis_for_pins([(pin, location) for pin, location, _ in needs_wiki])
        wiki_rows: dict[int, Boundary] = {}
        if wiki_of:
            for row in self.filter(wiki_id__in=set(wiki_of.values()), pin__isnull=True, boundary_type=boundary_type):
                wiki_rows.setdefault(row.wiki_id, row)
        for waiting, waiting_location, waiting_scoped in needs_wiki:
            wiki_id = wiki_of.get(waiting.pk)
            answers[waiting.pk] = self._resolve_pin_tail(boundary_type, location=waiting_location, scoped=waiting_scoped, wiki_row=wiki_rows.get(wiki_id) if wiki_id else None)[0]
        return answers

    @staticmethod
    def _wikis_for_pins(pins: list[tuple[Pin, Location | None]]) -> dict[int, int]:
        """Each pin's wiki id - its own, else its location's (``Wiki.objects.existing_for_location``) - in up to two queries.

        Args:
            pins: Pins paired with their locations.

        Returns:
            Pin id to wiki id, for pins that have one.
        """
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki_of = {pin.pk: pin.wiki_id for pin, _ in pins if pin.wiki_id}
        unlinked = [(pin, location) for pin, location in pins if not pin.wiki_id and location is not None]
        if not unlinked:
            return wiki_of
        by_location: dict[int, int] = {}
        by_place: dict[int, int] = {}
        location_ids = {location.pk for _, location in unlinked}
        place_ids = {location.place_id for _, location in unlinked if location.place_id}
        for wiki_id, location_id, place_id in Wiki.objects.filter(Q(location_id__in=location_ids) | Q(place_id__in=place_ids)).order_by("pk").values_list("pk", "location_id", "place_id"):
            if location_id in location_ids:
                by_location.setdefault(location_id, wiki_id)
            if place_id in place_ids:
                by_place.setdefault(place_id, wiki_id)
        for pin, location in unlinked:
            wiki_id = by_location.get(location.pk) or (by_place.get(location.place_id) if location.place_id else None)
            if wiki_id:
                wiki_of[pin.pk] = wiki_id
        return wiki_of

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
