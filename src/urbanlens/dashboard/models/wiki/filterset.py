from __future__ import annotations

import django_filters
from django_filters import CharFilter, NumberFilter

from urbanlens.dashboard.models.wiki.model import Wiki


class WikiFilter(django_filters.FilterSet):
    categories = CharFilter(method="by_category")
    by_name = CharFilter(method="by_name")
    by_created_year = NumberFilter(method="by_created_year")
    by_updated_year = NumberFilter(method="by_updated_year")

    class Meta:
        model = Wiki
        fields = [
            "name",
            "categories",
            "by_name",
            "by_created_year",
            "by_updated_year",
        ]
