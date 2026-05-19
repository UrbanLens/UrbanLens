from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from rest_framework.viewsets import GenericViewSet

logger = logging.getLogger(__name__)


class HealthController(GenericViewSet):
    def check(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("Okay!", status=200)
