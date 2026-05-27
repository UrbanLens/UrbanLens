"""CategoryFilter - backed by Badge with kind='category'."""

from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.badges.model import Badge


class CategoryFilter(django_filters.FilterSet):
    """FilterSet for Badge rows that represent categories."""

    class Meta:
        model = Badge
        fields = ["name"]
