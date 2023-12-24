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
*        Path:    /model.py                                                                                            *
*        Project: locations                                                                                            *
*        Version: <<projectversion>>                                                                                   *
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
from djangofoundry.models.fields import CharField, DecimalField, ForeignKey
# App Imports
from dashboard.models import abstract
from dashboard.models.categories.model import Category
from dashboard.models.locations.queryset import Manager
from dashboard.models.tags.model import Tag

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
    pin_icon = ImageField()
    icon = ImageField(null=True, blank=True)
    status = IntegerField(choices=STATUS_CHOICES, default=WISH_TO_VISIT)

    def change_category(self, category_id):
        category = Category.objects.get(id=category_id)
        self.categories.clear()
        self.categories.add(category)
        self.save()

    profile = ForeignKey(
        'dashboard.Profile', 
        on_delete=CASCADE, 
        related_name='locations'
    )
    tags = ManyToManyField(
        Tag,
        blank=True
    )

    objects = Manager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_locations'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['name']),
            Index(fields=['icon']),
            Index(fields=['priority']),
            Index(fields=['last_visited']),
            Index(fields=['latitude', 'longitude']),
        ]
