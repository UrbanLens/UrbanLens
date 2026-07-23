"""Health check endpoint for Docker / load-balancer probes."""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from rest_framework.permissions import AllowAny
from rest_framework.viewsets import GenericViewSet

logger = logging.getLogger(__name__)


class HealthController(GenericViewSet):
    """Liveness probe used by docker-compose healthchecks.

    Must remain unauthenticated: compose probes with
    ``curl -f http://localhost:8000/health/`` (no session cookie), and a
    non-2xx response marks the ``app`` container unhealthy, which blocks
    ``app-ws`` (Daphne) from starting via ``depends_on: service_healthy``.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def check(self, request: HttpRequest) -> HttpResponse:
        """Return a plain 200 so probes can treat any other status as failure.

        Args:
            request: Incoming HTTP request (unused).

        Returns:
            ``HttpResponse`` with body ``Okay!`` and status 200.
        """
        return HttpResponse("Okay!", status=200)
