"""CategoryViewSet - backed by Label.objects.categories()."""

from __future__ import annotations

from rest_framework import viewsets

from urbanlens.dashboard.models.categories.serializer import CategorySerializer
from urbanlens.dashboard.models.labels.model import Label


class CategoryViewSet(viewsets.ModelViewSet):
    """ViewSet for Label rows with kind='category'."""

    queryset = Label.objects.categories().all()
    serializer_class = CategorySerializer
