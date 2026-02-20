import logging

from django.http import HttpResponse
from rest_framework.viewsets import GenericViewSet

logger = logging.getLogger(__name__)


class HealthController(GenericViewSet):
    def check(self, request, *args, **kwargs):
        return HttpResponse("Okay!", status=200)
