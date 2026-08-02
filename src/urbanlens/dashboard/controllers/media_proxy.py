"""Server-side proxies for Media-gallery photo sources whose URLs require a private API key.

Never expose the underlying provider URL (and its embedded key) directly to
the browser - these views fetch the bytes server-side and cache them briefly
so repeated views/pagination don't re-hit the upstream API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.core.signing import Signer
from django.http import HttpResponse
from django.views import View
import requests

from urbanlens.dashboard.controllers.media_auth import CredentialOrSessionMediaMixin, MediaThrottledError
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

#: Salt for the proxy-URL signature. The photo_name path segment is otherwise
#: fully client-controlled: any logged-in user could replay copied/guessed
#: photo references and consume Places quota through this proxy. Signing binds
#: each URL to a photo name the server itself put in a LocationCache row
#: (see GoogleMapsPhotosPanelSource.media_items), without needing a per-photo
#: database lookup here. Deterministic (no timestamp) so the URL for a given
#: photo stays stable and cacheable.
_PHOTO_SIGNER_SALT = "urbanlens.media_proxy.google_maps_photo"


def sign_photo_name(photo_name: str) -> str:
    """Signature token binding a proxy URL to a server-issued photo name.

    Args:
        photo_name: The raw (unquoted) Places API photo name.

    Returns:
        The URL-safe signature to pass as the proxy URL's ``sig`` param.
    """
    return Signer(salt=_PHOTO_SIGNER_SALT).signature(photo_name)


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


class GoogleMapsPhotoProxyView(CredentialOrSessionMediaMixin, View):
    """GET media-photo/google-maps/<photo_name>/ - proxies one Google Maps place photo.

    Accepts either a logged-in session (the browser rendering a pin's Media
    gallery) or an external API credential holding ``media:read`` (the mobile
    client rendering the same gallery with no session cookie at all) - that
    half is
    :class:`~urbanlens.dashboard.controllers.media_auth.CredentialOrSessionMediaMixin`,
    shared with the authenticated media gate. It was previously
    ``LoginRequiredMixin`` alone, which made every panel image on the pin
    detail screen simply unreachable to an API client.

    The mixin is called explicitly rather than through ``dispatch()`` because
    this view's ordering is load-bearing - see :meth:`get`.
    """

    def get(self, request: HttpRequest, photo_name: str) -> HttpResponseBase:
        """Serve one Places photo's bytes, from cache or from the upstream API.

        The order of the checks below is the security-relevant part:

        1. **Signature.** ``photo_name`` is fully client-controlled and this
           view will fetch it from Google, so without the signature the
           endpoint is an open image-fetching relay charged to the site's own
           Places quota. It is therefore rejected before any credential is
           read, any cache entry is touched, or any throttle is consulted -
           authentication must never become the thing that makes an unsigned
           reference fetchable.
        2. **Identity.** Only then is the requester resolved, and it is
           resolved *before* the cache read below, which returns real image
           bytes: gating after it would turn possession of a signed URL alone
           into anonymous access to every photo anyone had already fetched.
        3. **The external-lookups opt-out**, which guards only the upstream
           call - a cache hit costs no quota, so an opted-out user still sees
           imagery the site already holds.

        Args:
            request: The current request, carrying either a session or an
                external API credential.
            photo_name: The Places photo reference from the URL, still
                percent-encoded as Django's ``<path:>`` converter handed it
                over (untrusted until the signature check passes).

        Returns:
            The image bytes with the upstream content type; 404 for an
            unsigned/tampered URL, an expired reference, an unresolvable
            credential, or a requester who opted out of external lookups; 429
            for a credential over its media budget; 502 when the upstream
            provider genuinely failed; or a login redirect for an anonymous
            browser request.
        """
        # The producer signs the RAW name but reverses the URL with the name
        # percent-encoded (photo names contain slashes), and Django's <path:>
        # converter hands the still-encoded segment through - so accept the
        # signature against either form rather than caring which decoding
        # depth this deployment's URL stack landed on.
        from urllib.parse import unquote

        from urbanlens.dashboard.services.apis.locations import places_resolution
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        sig = request.GET.get("sig", "")
        if not (hmac.compare_digest(sig, sign_photo_name(photo_name)) or hmac.compare_digest(sig, sign_photo_name(unquote(photo_name)))):
            return HttpResponse(status=404)

        try:
            profile = self.resolve_media_profile(request)
        except MediaThrottledError:
            return self.media_throttled_response()
        if profile is None:
            return self.media_auth_failure_response(request)

        cache_key = f"ul_gmaps_photo_{hashlib.sha256(photo_name.encode()).hexdigest()}"
        cached = cache.get(cache_key)
        if cached == _EXPIRED_SENTINEL:
            return HttpResponse(status=404)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        redata_configured = bool(settings.redata_api_url and settings.redata_api_key)
        if not settings.google_unrestricted_api_key and not redata_configured:
            return HttpResponse(status=404)
        # Serving from cache above is free, but an upstream fetch consumes the
        # site's Places quota on this requester's behalf - honor their own
        # external-lookups opt-out for the actual API call. Read off the
        # resolved profile rather than ``request.user``: on a credential-
        # authenticated request there is no session user to hang a profile off
        # at all, and the opt-out belongs to the person, not to whichever
        # client they happened to use.
        if not profile.external_apis_enabled:
            return HttpResponse(status=404)
        try:
            content, content_type = places_resolution.download_photo(photo_name, api_key=settings.google_unrestricted_api_key or "")
        except places_resolution.PhotoNotFoundError:
            # Confirmed gone (either provider) - same treatment as a Google
            # 404 below: an ordinary, expected condition, not a server error.
            logger.info("Photo reference expired for %r", photo_name)
            cache.set(cache_key, _EXPIRED_SENTINEL, _EXPIRED_CACHE_TTL)
            return HttpResponse(status=404)
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
        except (requests.exceptions.RequestException, GatewayRequestError, ValueError):
            logger.exception("Places photo media request failed for %r", photo_name)
            return HttpResponse(status=502)
        cache.set(cache_key, (content, content_type), _PHOTO_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)
