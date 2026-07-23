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
#: How long a *confirmed-expired* photo reference is cached as gone, before
#: trying the upstream again - shorter than the success TTL above so a
#: reference that only briefly 404s (rather than being permanently expired -
#: Google's photo references are, in practice, essentially always permanently
#: gone once they 404, but there's no documented guarantee of that) isn't
#: treated as gone forever.
_EXPIRED_CACHE_TTL = 6 * 3600
#: Cache sentinel for "confirmed 404 from upstream", distinguishing it from
#: the (content, content_type) tuple a successful fetch caches.
_EXPIRED_SENTINEL = "expired"


class GoogleMapsPhotoProxyView(LoginRequiredMixin, View):
    """GET media-photo/google-maps/<photo_name>/ - proxies one Google Maps place photo."""

    def get(self, request: HttpRequest, photo_name: str) -> HttpResponse:
        from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway

        cache_key = f"ul_gmaps_photo_{hashlib.sha256(photo_name.encode()).hexdigest()}"
        cached = cache.get(cache_key)
        if cached == _EXPIRED_SENTINEL:
            return HttpResponse(status=404)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        if not settings.google_unrestricted_api_key:
            return HttpResponse(status=404)
        # Serving from cache above is free, but an upstream fetch consumes the
        # site's Places quota on this requester's behalf - honor their own
        # external-lookups opt-out for the actual API call.
        from urbanlens.dashboard.models.profile.model import Profile

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.external_apis_enabled:
            return HttpResponse(status=404)
        try:
            content, content_type = GooglePlacesGateway(api_key=settings.google_unrestricted_api_key).get_photo_media(photo_name)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # Google Places photo references expire over time (not
                # documented how long they're valid for, but it happens
                # routinely for older cached media) - this is expected,
                # ordinary behavior, not a server error: 404 to the client
                # (not 502, which misleadingly implies *we* failed to reach
                # Google), logged quietly, and cached so a stale reference
                # embedded in old cached media doesn't keep re-hitting the
                # upstream API on every view.
                logger.info("Google Places photo reference expired for %r", photo_name)
                cache.set(cache_key, _EXPIRED_SENTINEL, _EXPIRED_CACHE_TTL)
                return HttpResponse(status=404)
            logger.exception("Google Places photo media request failed for %r -> Status Code: %s, Body: %s", photo_name, e.response.status_code if e.response is not None else "?", e.response.text if e.response is not None else "")
            return HttpResponse(status=502)
        except requests.exceptions.RequestException:
            logger.exception("Google Places photo media request failed for %r", photo_name)
            return HttpResponse(status=502)
        cache.set(cache_key, (content, content_type), _PHOTO_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)
