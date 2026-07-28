# Generic imports
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

# Django Imports
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Q

# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def quantize_coordinate(value: float | str | Decimal, field_name: str) -> Decimal:
    """Round a submitted coordinate to the precision ``Location`` actually stores.

    ``Location.latitude``/``longitude`` are fixed-precision decimals, so the
    database rounds on insert. Rounding here first means a lookup compares the
    same value the row will hold, rather than the caller's raw float - which is
    what makes exact-coordinate matching agree with the ``(latitude, longitude)``
    unique constraint instead of racing it.

    Args:
        value: The submitted coordinate.
        field_name: Which Location field it is - the precision comes from the
            field itself rather than a duplicated constant.

    Returns:
        The coordinate at the field's own decimal precision.

    Raises:
        TypeError: The named field isn't a fixed-precision decimal, so there is
            no precision to round to - the assumption this rests on, worth
            failing loudly rather than silently mis-rounding.
    """
    from urbanlens.dashboard.models.location.model import Location

    field = Location._meta.get_field(field_name)  # noqa: SLF001 - _meta is public API despite the underscore
    if not isinstance(field, DecimalField) or field.decimal_places is None:
        raise TypeError(f"Location.{field_name} must be a fixed-precision DecimalField.")
    return Decimal(str(float(value))).quantize(Decimal(1).scaleb(-field.decimal_places), rounding=ROUND_HALF_UP)


class LocationQuerySet(abstract.PublicDashboardQuerySet):
    """QuerySet for Location - the shared, user-agnostic half of the place model.

    Filters here operate on global place data (coordinates, name, CID, address).
    For per-user filtering (by profile, visit status, priority) use PinQuerySet.
    """

    def by_latitude(self, latitude):
        return self.filter(latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(longitude=longitude)

    def by_cid(self, cid: int):
        return self.filter(google_place__cid=cid)

    def by_official_name(self, name):
        return self.filter(official_name__icontains=name)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def _boundary_polygon_q(self, pt) -> Q:
        """Q expression matching Locations whose default Boundary *generated* polygon contains pt.

        Only location-default rows and only `generated_polygon` (API-derived)
        are used for matching. User/community-drawn polygons are excluded so a
        boundary can't be inflated by an edit to capture unrelated pins. Both
        property and building boundaries count - a point inside a building is
        on that building's property.
        """
        return Q(boundaries__pin__isnull=True) & Q(boundaries__wiki__isnull=True) & Q(boundaries__profile__isnull=True) & Q(boundaries__source="") & Q(boundaries__generated_polygon__contains=pt)

    def _locations_without_boundary_polygon(self):
        """Return locations that have no default *generated* boundary polygon."""
        from django.db.models import Subquery

        from urbanlens.dashboard.models.boundary.model import Boundary

        with_polygon = Boundary.objects.location_defaults().filter(generated_polygon__isnull=False).values("location_id")
        return self.exclude(pk__in=Subquery(with_polygon))

    def within_bounding_box(self, latitude: float, longitude: float):
        """Return Locations whose default Boundary *generated* polygon contains this coordinate.

        Only the API-derived `generated_polygon` on location-default Boundary
        rows is considered, never a user/community-drawn `polygon`. A drawn
        boundary can be inflated to capture unrelated pins, so it must not
        influence location matching. Falls back to a 50 m proximity check (the
        default circle boundary) for Locations that have no generated polygon
        at all, mirroring ``LocationManager.get_all_for_point``.
        """
        from django.contrib.gis.geos import Point as GEOSPoint

        pt = GEOSPoint(float(longitude), float(latitude), srid=4326)
        in_boundary = self.filter(self._boundary_polygon_q(pt)).distinct()
        if in_boundary.exists():
            return in_boundary
        return self._locations_without_boundary_polygon().filter(point__distance_lte=(pt, D(m=50))).distinct()

    def filter_by_criteria(self, criteria):
        query = Q()
        if criteria.get("date_added"):
            query &= Q(created__date=criteria["date_added"])
        return self.filter(query)


class LocationManager(abstract.PublicDashboardManager.from_queryset(LocationQuerySet)):
    """Manager for Location. Use get_for_point to find a Location whose Boundary polygon contains a coordinate."""

    def get_for_point(self, latitude: float, longitude: float):
        """Return the first Location whose default Boundary generated polygon contains (lat, lon), or None.

        Falls back to a 50 m proximity check for Locations that have no boundary polygon.
        """
        return self.within_bounding_box(latitude, longitude).first()

    def get_all_for_point(self, latitude: float, longitude: float) -> Self:
        """Return ALL Locations whose default Boundary generated polygon contains (lat, lon) as a QuerySet.

        Unlike get_for_point, this returns every match so callers can detect when a
        coordinate falls inside multiple boundary polygons (ambiguous location).  Falls
        back to 50 m proximity for Locations without a boundary polygon only when there
        are no polygon matches at all.

        Args:
            latitude: WGS-84 latitude of the point to test.
            longitude: WGS-84 longitude of the point to test.

        Returns:
            QuerySet of matching Location rows, ordered by name.  May be empty.
        """
        return self.within_bounding_box(latitude, longitude)

    def get_exact_or_create(self, latitude, longitude, defaults=None) -> tuple[Location, bool]:
        """Get or create the Location at exactly these coordinates.

        The counterpart to :meth:`get_nearby_or_create` for callers that must
        keep a submitted point exactly as given - a detail pin's own marker, a
        child wiki, a manual pin move - rather than snapping it onto whatever
        Location happens to sit within the dedup radius.

        Matches on the stored coordinates rather than a zero-distance PostGIS
        probe. Those are not equivalent: ``point`` is built from the raw float
        while ``latitude``/``longitude`` are rounded to the fields' precision on
        insert, so two submissions differing below that precision have points a
        few centimetres apart but the *same* stored pair. A zero-distance probe
        misses the existing row and the insert then trips the
        ``(latitude, longitude)`` unique constraint; matching on the stored
        values is what that constraint actually enforces.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            defaults: Field values applied only when a row is created.

        Returns:
            Tuple of (Location, whether it was created).
        """
        latitude_value = quantize_coordinate(latitude, "latitude")
        longitude_value = quantize_coordinate(longitude, "longitude")

        existing = self.filter(latitude=latitude_value, longitude=longitude_value).first()
        if existing is not None:
            return existing, False
        try:
            # Nested atomic so the raced insert fails inside its *own*
            # savepoint. Callers run this inside `transaction.atomic()` blocks
            # (pin moves, in both `PinViewSet.partial_update` and
            # `PinDetailView.patch`), and an IntegrityError raised in an outer
            # transaction marks the whole thing for rollback - so catching it
            # here without a savepoint left the connection in a broken state
            # and the recovery query below raised TransactionManagementError
            # instead of returning the winning row. The bare `except` turned a
            # survivable race into a 500 in exactly the case it was written to
            # survive.
            with transaction.atomic():
                return self.create(latitude=latitude_value, longitude=longitude_value, **(defaults or {})), True
        except IntegrityError:
            # A concurrent request inserted this coordinate pair between the
            # lookup and the insert - use that row rather than surfacing a 500.
            existing = self.filter(latitude=latitude_value, longitude=longitude_value).first()
            if existing is None:
                raise
            return existing, False

    def get_nearby_or_create(self, latitude, longitude, threshold_meters=50, defaults=None):
        """
        Get or create a Location instance, considering two locations the same if they are within a certain distance threshold.

        Args:
            latitude (float): Latitude of the location.
            longitude (float): Longitude of the location.
            threshold_meters (float): Distance threshold in meters for considering locations as the same.
            defaults (dict, optional): Defaults to use for object creation.

        Returns:
            (Location, bool): Tuple of (Location instance, created boolean)

        """
        point = Point(longitude, latitude, srid=4326)

        # Find existing locations within the threshold distance
        existing_locations = self.filter(
            point__distance_lte=(point, D(m=threshold_meters)),
        )

        if existing_locations.exists():
            # Return the first close enough location and False for 'created'
            return existing_locations.first(), False

        # No existing location found within the threshold, create a new one
        location_data = {
            "latitude": latitude,
            "longitude": longitude,
            **(defaults or {}),
        }
        try:
            # Nested atomic for the same reason as get_or_create_at_coordinates:
            # without its own savepoint the IntegrityError poisons any
            # enclosing transaction, and the recovery query below then raises
            # TransactionManagementError instead of returning the winner.
            with transaction.atomic():
                location = self.create(**location_data)
        except IntegrityError:
            # A concurrent request created a Location at these exact coordinates between
            # the existence check above and this insert (the (latitude, longitude)
            # unique_together constraint) - return that row instead of letting the race
            # surface as an unhandled 500.
            existing_locations = self.filter(point__distance_lte=(point, D(m=threshold_meters)))
            if existing_locations.exists():
                return existing_locations.first(), False
            raise

        # Return the new location and True for 'created'
        return location, True
