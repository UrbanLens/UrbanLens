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
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
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
from django.db.models import Index
from django.db.models.fields import CharField, DateTimeField
from django.db.models import ManyToManyField

# App Imports
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.queryset import PinManager
from urbanlens.dashboard.models.profile import Profile

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    pass

logger = logging.getLogger(__name__)

class Trip(abstract.Model):
    """
    Records trip data.
    """
    name = CharField(max_length=255, blank=True, null=True)
    description = CharField(max_length=500, null=True, blank=True)
    start_date = DateTimeField(null=True, blank=True)
    end_date = DateTimeField(null=True, blank=True)

    profiles = ManyToManyField(
        Profile,
        blank=True,
        related_name='trips',
    )

    pins = ManyToManyField(
        'urbanlens.dashboard.Pin',
        blank=True,
        default=list
    )

    objects = PinManager()

    def __str__(self):
        pins = ', '.join([str(pin) for pin in self.pins.all()]) if self.pins.all() else []
        return f"Name: {self.name}\nDescription: {self.description or ''}\nStart Date: {self.start_date}\nEnd Date: {self.end_date}\nLocations: {pins}"

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
            'pins': [pin.id for pin in self.pins.all()],
        }

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_trips'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['start_date']),
            Index(fields=['end_date']),
        ]
