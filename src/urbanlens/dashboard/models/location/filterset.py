"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    filterset.py                                                                                         *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2025-03-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess.a.mann@gmail.com                                                                                *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2025-03-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import django_filters
from django_filters import CharFilter, NumberFilter
from urbanlens.dashboard.models.location.model import Location

class LocationFilter(django_filters.FilterSet):
    categories = CharFilter(method='by_category')
    by_latitude = NumberFilter(method='by_latitude')
    by_longitude = NumberFilter(method='by_longitude')
    by_name = CharFilter(method='by_name')
    by_created_year = NumberFilter(method='by_created_year')
    by_updated_year = NumberFilter(method='by_updated_year')

    class Meta:
        model = Location
        fields = [
            'name',
            'categories',
            'latitude',
            'longitude',
            'by_latitude',
            'by_longitude',
            'by_name',
            'by_created_year',
            'by_updated_year',
        ]
