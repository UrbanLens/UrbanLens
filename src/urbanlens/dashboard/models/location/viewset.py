from __future__ import annotations

import logging

from rest_framework import viewsets

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.serializer import LocationSerializer

logger = logging.getLogger(__name__)


class LocationViewSet(viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    basename = "locations"

    def get_queryset(self):
        return Location.objects.none()
