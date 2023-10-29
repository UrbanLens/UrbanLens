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
# Django Imports
# App Imports
from dashboard.models import abstract

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

from django.db.models import Q
from datetime import datetime

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

    def by_description(self, description):
        return self.filter(description__icontains=description)

    def by_icon(self, icon):
        return self.filter(icon=icon)

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def by_user(self, user):
        return self.filter(user=user)

    def by_created_date(self, created_date):
        return self.filter(created__date=created_date)

    def by_updated_date(self, updated_date):
        return self.filter(updated__date=updated_date)

    def by_pin_icon(self, pin_icon):
        return self.filter(pin_icon=pin_icon)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def by_created_month(self, month):
        return self.filter(created__month=month)

    def by_updated_month(self, month):
        return self.filter(updated__month=month)

    def by_created_day(self, day):
        return self.filter(created__day=day)

    def by_updated_day(self, day):
        return self.filter(updated__day=day)

class Manager(abstract.Manager.from_queryset(QuerySet)):
    '''
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    '''
