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
from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS
from urbanlens.dashboard.models.place.queryset import point_for_coordinates

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place

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

    def in_domain_of(self, place: Place | None) -> Self:
        """Locations resolving onto any place in ``place``'s access domain."""
        if place is None:
            return self.none()
        return self.filter(place__domain_root_id=place.domain_root_id)

    def within_bounding_box(self, latitude: float, longitude: float) -> Self:
        """Locations sharing the access domain of whatever is at this coordinate.

        Resolution happens once, on Place, and the answer is a single
        real-world thing - so "which locations cover this point?" is no longer
        a geometry query over every location that ever copied a polygon, but a
        lookup of the domain the point resolves into.

        That is the fix for the campus case: importing 124 buildings used to
        give 124 Locations their own copy of the same parcel outline, so every
        point on the property matched all 125 at once. They now share one
        domain and answer as one place.

        Falls back to a 50 m proximity check for coordinates on no known
        place, which is the pre-Place behaviour for locations nobody has
        official geometry for.
        """
        from urbanlens.dashboard.models.place.model import Place

        pt = point_for_coordinates(latitude, longitude)
        if pt is None:
            return self.none()
        place = Place.objects.resolve_for_point(latitude, longitude)
        if place is not None:
            return self.filter(place__domain_root_id=place.domain_root_id).distinct()
        return self.filter(place__isnull=True).filter(point__distance_lte=(pt, D(m=DEFAULT_RADIUS_METERS))).distinct()

    def filter_by_criteria(self, criteria):
        query = Q()
        if criteria.get("date_added"):
            query &= Q(created__date=criteria["date_added"])
        return self.filter(query)


class LocationManager(abstract.PublicDashboardManager.from_queryset(LocationQuerySet)):
    """Manager for Location. Use get_for_point to find the Location standing at a coordinate."""

    def get_for_point(self, latitude: float, longitude: float):
        """Return the first Location sharing the access domain at (lat, lon), or None.

        Falls back to a 50 m proximity check for coordinates on no known place.
        """
        return self.within_bounding_box(latitude, longitude).first()

    def get_all_for_point(self, latitude: float, longitude: float) -> Self:
        """Return every Location sharing the access domain at (lat, lon).

        These are locations describing the *same* real-world thing - one
        parcel and the buildings on it - not competing candidates. For the
        genuinely ambiguous case (two unrelated parcels whose county geometry
        overlaps) see ``Place.objects.competing_for_point``.

        Args:
            latitude: WGS-84 latitude of the point to test.
            longitude: WGS-84 longitude of the point to test.

        Returns:
            QuerySet of matching Location rows. May be empty.
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

    def get_nearby_or_create(self, latitude, longitude, threshold_meters=0, defaults=None):
        """
        Get or create a Location instance, optionally treating nearby coordinates as the same.

        The threshold now defaults to **exact**. Consolidating two drops at one
        real place is the *place's* job: they resolve onto the same parcel and
        share its wiki, its community, and its "places in common" entry without
        either coordinate being thrown away. Snapping discarded whichever
        coordinate arrived second - including when the first belonged to a
        different user - which defeated the 6-decimal precision Location goes
        to some trouble to store and enforce.

        A non-zero threshold remains available for the few callers whose job
        genuinely is radius matching (see
        ``services.apis.locations.legacy_cid_coordinate_fix``).

        Args:
            latitude (float): Latitude of the location.
            longitude (float): Longitude of the location.
            threshold_meters (float): Distance threshold in meters for considering locations as the same.
            defaults (dict, optional): Defaults to use for object creation.

        Returns:
            (Location, bool): Tuple of (Location instance, created boolean)

        """
        if not threshold_meters:
            return self.get_exact_or_create(latitude, longitude, defaults=defaults)

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
