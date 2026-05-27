"""CategoryViewSet - backed by Badge.objects.categories()."""

from __future__ import annotations

from rest_framework import viewsets

from urbanlens.dashboard.models.categories.serializer import CategorySerializer
from urbanlens.dashboard.models.badges.model import Badge


class CategoryViewSet(viewsets.ModelViewSet):
    """ViewSet for Badge rows with kind='category'."""

    queryset = Badge.objects.categories().all()
    serializer_class = CategorySerializer
