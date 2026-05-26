"""CategoryFilter - backed by Tag with kind='category'."""

from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.tags.model import Tag


class CategoryFilter(django_filters.FilterSet):
    """FilterSet for Tag rows that represent categories."""

    class Meta:
        model = Tag
        fields = ["name"]
