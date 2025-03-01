"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    geocoding.py                                                                                         *
*        Path:    /dashboard/models/cache/geocoding.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations
# Django Imports
from django.db.models import Index
# 3rd Party Imports
from django.db.models.fields import CharField, DecimalField

# App Imports
from urbanlens.dashboard.models import abstract

class GeocodedLocation(abstract.Model):
    """
    Records geocoded location data.
    """
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    place_name = CharField(max_length=255, null=True, blank=True)
    json_response = CharField(max_length=50000, null=True, blank=True)

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_geocoded_locations'
        get_latest_by = 'updated'
        indexes = [
            Index(fields=['latitude', 'longitude']),
            Index(fields=['place_name'])
        ]