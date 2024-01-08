"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/trips/model.py                                                                     *
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
from typing import TYPE_CHECKING
import logging
# Django Imports
from django.db.models import Index, CASCADE
from django.db.models.fields import CharField, DateTimeField
from django.db.models import ForeignKey, ManyToManyField

# App Imports
from dashboard.models import abstract
from dashboard.models.locations.queryset import Manager

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    pass

logger = logging.getLogger(__name__)

class Trip(abstract.Model):
    """
    Records trip data.
    """
    name = CharField(max_length=255)
    description = CharField(max_length=500, null=True, blank=True)
    start_date = DateTimeField(null=True, blank=True)
    end_date = DateTimeField(null=True, blank=True)

    profile = ForeignKey(
        'dashboard.Profile', 
        on_delete=CASCADE, 
        related_name='trips'
    )
    locations = ManyToManyField(
        'dashboard.Location', 
        blank=True,
        default=list
    )

    objects = Manager()

    def __str__(self):
        locations = ', '.join([str(location) for location in self.locations.all()]) if self.locations.all() else []
        return f"Name: {self.name}\nDescription: {self.description or ''}\nStart Date: {self.start_date}\nEnd Date: {self.end_date}\nLocations: {locations}"

    def to_json(self):
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'start_date': self.start_date.isoformat() if self.start_date else "never",
            'end_date': self.end_date.isoformat() if self.end_date else "never",
            'profile': self.profile.id,
            'locations': [location.id for location in self.locations.all()],
        }

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_trips'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['start_date']),
            Index(fields=['end_date']),
        ]
