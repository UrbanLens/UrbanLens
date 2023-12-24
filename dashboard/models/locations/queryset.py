"""
    Metadata:

        File: queryset.py
        Project: UrbanLens
        Author: Jess Mann
        Email: jess@manlyphotos.com

        -----

        Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
from typing import TYPE_CHECKING
import logging
from datetime import datetime
# Django Imports
from django.db.models import Q
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
        return self.filter(distance__lte=radius)

class Manager(abstract.Manager.from_queryset(QuerySet)):
    '''
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    '''
from django.db.models import Q

class QuerySet(models.QuerySet):
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
