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
*        Path:    /dashboard/models/locations/model.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
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
from django.forms import ImageField
# 3rd Party Imports
from djangofoundry.models.fields import CharField, DecimalField, ForeignKey, IntegerField, DateTimeField, ManyToManyField
from djangofoundry.models import TextChoices

# App Imports
from dashboard.models import abstract
from dashboard.models.locations.queryset import Manager

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    pass

logger = logging.getLogger(__name__)

class LocationStatus(TextChoices):
    VISITED = 1
    WISH_TO_VISIT = 2

class Location(abstract.Model):
    """
    Records location data.
    """

    def __str__(self):
        return f"Name: {self.name}\nDescription: {self.description or ''}\nPriority: {self.priority}\nLast Visited: {self.last_visited}\nStatus: {LocationStatus(self.status).label}\nCategories: {', '.join([str(category) for category in self.categories.all()])}\nTags: {', '.join([str(tag) for tag in self.tags.all()])}"

    name = CharField(max_length=255)
    icon = CharField(max_length=255, null=True, blank=True)
    description = CharField(max_length=500, null=True, blank=True)
    priority = IntegerField(default=0)
    last_visited = DateTimeField(null=True, blank=True)
    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    pin_icon = ImageField()
    icon = CharField(max_length=255, null=True, blank=True)
    status = IntegerField(choices=LocationStatus.choices, default=LocationStatus.WISH_TO_VISIT)

    profile = ForeignKey(
        'dashboard.Profile', 
        on_delete=CASCADE, 
        related_name='locations'
    )
    categories = ManyToManyField(
        'dashboard.Category', 
        blank=True
    )
    tags = ManyToManyField(
        'dashboard.Tag',
        blank=True
    )

    objects = Manager()

    def change_category(self, category_id):
        from dashboard.models.categories.model import Category
        category = Category.objects.get(id=category_id)
        self.categories.clear()
        self.categories.add(category)
        self.save()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_locations'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['priority']),
            Index(fields=['last_visited']),
            Index(fields=['latitude', 'longitude']),
        ]
