"""CategoryFilter - backed by Label with kind='category'."""

from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.labels.model import Label


class CategoryFilter(django_filters.FilterSet):
    """FilterSet for Label rows that represent categories."""

    class Meta:
        model = Label
        fields = ["name"]
