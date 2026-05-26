"""CategoryViewSet - backed by Tag.objects.categories()."""

from __future__ import annotations

from rest_framework import viewsets

from urbanlens.dashboard.models.categories.serializer import CategorySerializer
from urbanlens.dashboard.models.tags.model import Tag


class CategoryViewSet(viewsets.ModelViewSet):
    """ViewSet for Tag rows with kind='category'."""

    queryset = Tag.objects.categories().all()
    serializer_class = CategorySerializer
