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
from django.db.models import Index, CASCADE
from django.forms import ImageField
# 3rd Party Imports
from djangofoundry.models.fields import CharField, DecimalField, InsertedNowField, UpdatedNowField, ForeignKey
# App Imports
from dashboard.models import abstract
from dashboard.models.locations.queryset import Manager
from dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    pass

logger = logging.getLogger(__name__)

class Location(abstract.Model):
    """
    Records location data.
    """
    from django.db.models import DateTimeField, IntegerField, ManyToManyField
    from dashboard.models.categories.model import Category

    VISITED = 1
    WISH_TO_VISIT = 2
    STATUS_CHOICES = [
        (VISITED, 'Visited'),
        (WISH_TO_VISIT, 'Wish to Visit'),
    ]

    name = CharField(max_length=255)
    icon = CharField(max_length=255)
    description = CharField(max_length=500, null=True, blank=True)
    categories = ManyToManyField(Category)
    priority = IntegerField()
    last_visited = DateTimeField(null=True, blank=True)
    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    profile = ForeignKey(Profile, on_delete=CASCADE, related_name='locations')
    pin_icon = ImageField(upload_to='pin_icons/', null=True, blank=True)
    from django.db.models import ManyToManyField
    from dashboard.models.tags.model import Tag

    status = IntegerField(choices=STATUS_CHOICES, default=WISH_TO_VISIT)
    tags = ManyToManyField(Tag, blank=True)

    objects = Manager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_locations'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['icon']),
            Index(fields=['categories']),
            Index(fields=['priority']),
            Index(fields=['last_visited']),
            Index(fields=['latitude', 'longitude']),
        ]
