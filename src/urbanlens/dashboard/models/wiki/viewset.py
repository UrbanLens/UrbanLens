from __future__ import annotations

import logging

from rest_framework import viewsets

from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.serializer import WikiSerializer

logger = logging.getLogger(__name__)


class WikiViewSet(viewsets.ModelViewSet):
    serializer_class = WikiSerializer
    basename = "wikis"

    def get_queryset(self):
        return Wiki.objects.none()
