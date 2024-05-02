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
*        Path:    /dashboard/models/locations/filterset.py                                                             *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import django_filters
from django_filters import CharFilter, NumberFilter
from dashboard.models.locations.model import Location

class LocationFilter(django_filters.FilterSet):
    categories = CharFilter(method='by_category')
    by_priority = NumberFilter(method='by_priority')
    by_latitude = NumberFilter(method='by_latitude')
    by_longitude = NumberFilter(method='by_longitude')
    by_name = CharFilter(method='by_name')
    by_created_year = NumberFilter(method='by_created_year')
    by_updated_year = NumberFilter(method='by_updated_year')
    rated = NumberFilter(method='rated')
    rated_over = NumberFilter(method='rated_over')
    rated_under = NumberFilter(method='rated_under')

    class Meta:
        model = Location
        fields = [
            'name',
            'icon',
            'categories',
            'priority',
            'last_visited',
            'latitude',
            'longitude',
            'rated',
            'rated_over',
            'rated_under',
            'by_priority',
            'by_latitude',
            'by_longitude',
            'by_name',
            'by_created_year',
            'by_updated_year',
        ]
