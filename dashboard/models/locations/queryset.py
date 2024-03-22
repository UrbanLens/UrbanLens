"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    queryset.py                                                                                          *
*        Path:    /dashboard/models/locations/queryset.py                                                              *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations
from typing import TYPE_CHECKING, Self
import logging
from datetime import datetime
# Django Imports
from django.db.models import Q
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models import PointField
from django.contrib.gis.measure import D
# App Imports
from dashboard.models import abstract

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

class QuerySet(abstract.QuerySet):
    '''
    A custom queryset. All models below will use this for interacting with results from the db.
    '''

    def never_visited(self):
        return self.filter(last_visited__isnull=True)

    def not_visited_this_year(self):
        return self.filter(last_visited__year__lt=datetime.now().year)

    def by_category(self, category):
        return self.filter(categories__name=category)

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_latitude(self, latitude):
        return self.filter(latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(longitude=longitude)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def by_user(self, user):
        return self.filter(user=user)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def nearby_locations(self, latitude, longitude, radius):
        from django.db.models import F
        from math import radians, sin, cos, sqrt, atan2
        R = 6371  # radius of the Earth in km
        lat1 = radians(latitude)
        lon1 = radians(longitude)
        lat2 = radians(F('latitude'))
        lon2 = radians(F('longitude'))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        distance = R * c
        return self.filter(distance__lte=distance)

    def filter_by_criteria(self, criteria):
        query = Q()
        if 'date_added' in criteria and criteria['date_added']:
            query &= Q(created__date=criteria['date_added'])
        if 'popularity' in criteria and criteria['popularity']:
            query &= Q(popularity__gte=criteria['popularity'])
        if 'tags' in criteria and criteria['tags']:
            tags = criteria['tags'].split(',')
            for tag in tags:
                query &= Q(tags__name__in=[tag])
        return self.filter(query)

    def rated(self, rating) -> Self:
        """
        Filters locations by the review.rating field
        """
        return self.filter(reviews__rating=rating)

    def rated_over(self, rating) -> Self:
        """
        Filters locations by the review.rating field
        """
        return self.filter(reviews__rating__gte=rating)

    def rated_under(self, rating) -> Self:
        """
        Filters locations by the review.rating field
        """
        return self.filter(reviews__rating__lte=rating)

class Manager(abstract.Manager.from_queryset(QuerySet)):
    '''
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    '''
    def get_nearby_or_create(self, latitude, longitude, profile, threshold_meters=50, defaults=None):
        """
        Get or create a Location instance, considering two locations the same if they are within a certain distance threshold.

        Args:
            latitude (float): Latitude of the location.
            longitude (float): Longitude of the location.
            profile (Profile): The profile associated with the location.
            threshold_meters (float): Distance threshold in meters for considering locations as the same.
            defaults (dict, optional): Defaults to use for object creation.

        Returns:
            (Location, bool): Tuple of (Location instance, created boolean)
        """
        point = Point(longitude, latitude, srid=4326)
        
        # Find existing locations within the threshold distance
        existing_locations = self.filter(
            location__distance_lte=(point, D(m=threshold_meters)),
            profile=profile
        )
        
        if existing_locations.exists():
            # Return the first close enough location and False for 'created'
            return existing_locations.first(), False
        
        # No existing location found within the threshold, create a new one
        location_defaults = {
            'location': point,
            **(defaults or {})
        }
        location = self.create(latitude=latitude, longitude=longitude, profile=profile, **location_defaults)
        
        # Return the new location and True for 'created'
        return location, True  
