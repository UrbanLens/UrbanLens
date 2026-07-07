from __future__ import annotations

import django_filters
from django_filters import CharFilter, NumberFilter

from urbanlens.dashboard.models.location.model import Location


class LocationFilter(django_filters.FilterSet):
    by_latitude = NumberFilter(method="by_latitude")
    by_longitude = NumberFilter(method="by_longitude")
    by_official_name = CharFilter(method="by_official_name")
    by_created_year = NumberFilter(method="by_created_year")
    by_updated_year = NumberFilter(method="by_updated_year")

    class Meta:
        model = Location
        fields = [
            "official_name",
            "latitude",
            "longitude",
            "by_latitude",
            "by_longitude",
            "by_official_name",
            "by_created_year",
            "by_updated_year",
        ]
