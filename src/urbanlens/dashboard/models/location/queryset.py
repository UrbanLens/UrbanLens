# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

# Django Imports
from django.db.models import Q

# App Imports
from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class LocationQuerySet(abstract.QuerySet):
    """QuerySet for Location - the shared, user-agnostic half of the place model.

    Filters here operate on global place data (coordinates, name, CID, address).
    For per-user filtering (by profile, visit status, priority) use PinQuerySet.
    """

    def by_category(self, category):
        return self.filter(categories__name=category)

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_latitude(self, latitude):
        return self.filter(latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(longitude=longitude)

    def by_cid(self, cid: int):
        return self.filter(cid=cid)

    def by_name(self, name):
        return self.filter(name__icontains=name)

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

    def within_bounding_box(self, latitude: float, longitude: float):
        """Return Locations whose bounding_box contains this coordinate."""
        from django.contrib.gis.geos import Point as GEOSPoint

        pt = GEOSPoint(float(longitude), float(latitude), srid=4326)
        return self.filter(bounding_box__contains=pt)

    def filter_by_criteria(self, criteria):
        query = Q()
        if criteria.get("date_added"):
            query &= Q(created__date=criteria["date_added"])
        if criteria.get("tags"):
            tags = criteria["tags"].split(",")
            for tag in tags:
                query &= Q(tags__name__in=[tag])
        return self.filter(query)


class LocationManager(abstract.Manager.from_queryset(LocationQuerySet)):
    """Manager for Location. Use get_for_point to find a Location whose bounding box contains a coordinate."""

    def get_for_point(self, latitude: float, longitude: float):
        """Return the first Location whose bounding_box contains (lat, lon), or None.

        Falls back to a 50 m proximity check for any Locations that have no
        bounding_box set (e.g. rows created before migration 0021).
        """
        from django.contrib.gis.geos import Point as GEOSPoint
        from django.contrib.gis.measure import D

        pt = GEOSPoint(float(longitude), float(latitude), srid=4326)
        # Primary: bounding-box containment
        match = self.filter(bounding_box__contains=pt).first()
        if match:
            return match
        # Fallback: proximity for legacy rows without bounding_box
        return self.filter(bounding_box__isnull=True, point__distance_lte=(pt, D(m=50))).first()

    def get_all_for_point(self, latitude: float, longitude: float) -> LocationQuerySet:
        """Return ALL Locations whose bounding_box contains (lat, lon) as a QuerySet.

        Unlike get_for_point, this returns every match so callers can detect when a
        coordinate falls inside multiple bounding boxes (ambiguous location).  Falls
        back to 50 m proximity for legacy rows without a bounding_box only when there
        are no bbox matches at all.

        Args:
            latitude: WGS-84 latitude of the point to test.
            longitude: WGS-84 longitude of the point to test.

        Returns:
            QuerySet of matching Location rows, ordered by name.  May be empty.
        """
        from django.contrib.gis.geos import Point as GEOSPoint
        from django.contrib.gis.measure import D

        pt = GEOSPoint(float(longitude), float(latitude), srid=4326)
        in_bbox = self.filter(bounding_box__contains=pt).order_by("name")
        if in_bbox.exists():
            return in_bbox
        return self.filter(bounding_box__isnull=True, point__distance_lte=(pt, D(m=50))).order_by("name")

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
