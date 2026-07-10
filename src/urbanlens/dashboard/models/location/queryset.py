# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

# Django Imports
from django.db.models import Q

# App Imports
from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


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

    def nearby_locations(self, latitude, longitude, radius):
        from math import atan2, cos, radians, sin, sqrt

        from django.db.models import F

        R = 6371  # radius of the Earth in km
        lat1 = radians(latitude)
        lon1 = radians(longitude)
        lat2 = radians(F("latitude"))
        lon2 = radians(F("longitude"))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        distance = R * c
        return self.filter(distance__lte=distance)

    def _boundary_polygon_q(self, pt) -> Q:
        """Q expression matching Locations whose default Boundary *generated* polygon contains pt.

        Only location-default rows and only `generated_polygon` (API-derived)
        are used for matching. User/community-drawn polygons are excluded so a
        boundary can't be inflated by an edit to capture unrelated pins. Both
        property and building boundaries count - a point inside a building is
        on that building's property.
        """
        return Q(boundaries__pin__isnull=True) & Q(boundaries__wiki__isnull=True) & Q(boundaries__profile__isnull=True) & Q(boundaries__generated_polygon__contains=pt)

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
        location = self.create(**location_data)

        # Return the new location and True for 'created'
        return location, True
