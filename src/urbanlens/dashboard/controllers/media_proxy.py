"""Server-side proxies for Media-gallery photo sources whose URLs require a private API key.

Never expose the underlying provider URL (and its embedded key) directly to
the browser - these views fetch the bytes server-side and cache them briefly
so repeated views/pagination don't re-hit the upstream API.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.views import View
import requests

from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_PHOTO_CACHE_TTL = 24 * 3600


class GoogleMapsPhotoProxyView(LoginRequiredMixin, View):
    """GET media-photo/google-maps/<photo_name>/ - proxies one Google Maps place photo."""

    def get(self, request: HttpRequest, photo_name: str) -> HttpResponse:
        from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway

        cache_key = f"ul_gmaps_photo_{hashlib.sha256(photo_name.encode()).hexdigest()}"
        cached = cache.get(cache_key)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        if not settings.google_unrestricted_api_key:
            return HttpResponse(status=404)
        try:
            content, content_type = GooglePlacesGateway(api_key=settings.google_unrestricted_api_key).get_photo_media(photo_name)
        except requests.exceptions.HTTPError as e:
            logger.exception("Google Places photo media request failed for %r -> Status Code: %s, Body: %s", photo_name, e.response.status_code, e.response.text)
            return HttpResponse(status=502)
        except requests.exceptions.RequestException:
            logger.exception("Google Places photo media request failed for %r", photo_name)
            return HttpResponse(status=502)
        cache.set(cache_key, (content, content_type), _PHOTO_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)
