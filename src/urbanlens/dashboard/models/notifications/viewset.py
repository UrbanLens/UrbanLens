from __future__ import annotations

from rest_framework import filters

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.notifications.serializer import Serializer


class ViewSet(abstract.ViewSet):
    serializer_class = Serializer
    queryset = NotificationLog.objects.all()
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["id"]  # ['-updated', '-created', 'status', 'guid']
    ordering = ["id"]  # ['-updated', '-created', 'id']
