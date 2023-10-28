"""
    Metadata:

        File: model.py
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
from django.db.models import Index
# 3rd Party Imports
from djangofoundry.models.fields import CharField, DecimalField, InsertedNowField, UpdatedNowField
# App Imports
from dashboard.models import abstract
from dashboard.models.locations.queryset import Manager

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    pass

logger = logging.getLogger(__name__)

class Location(abstract.Model):
    """
    Records location data.
    """
    name = CharField(max_length=255)
    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    created = InsertedNowField()
    updated = UpdatedNowField()

    objects = Manager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_locations'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['latitude', 'longitude']),
        ]
