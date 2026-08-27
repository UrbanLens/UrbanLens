"""Generic server-side preview endpoint for non-web-renderable gallery items.

The Media gallery puts external items straight into an ``<img>``, which fails
for the TIFFs, scanned PDFs and HEICs that archival providers routinely
return. This view fetches one such source URL server-side and returns a
browser-renderable JPEG/PNG of it (see ``services.media.previews``).

The URL is fully client-supplied and this view will fetch it, so - exactly
like ``media_proxy.GoogleMapsPhotoProxyView`` - it is gated on a signature the
server itself issued when it rendered the item into a gallery. Without that,
this is an open image-fetching relay: an SSRF vector and a way to launder
outbound requests through the site. The signature is checked before anything
else happens, and the fetch is additionally SSRF-validated on every redirect
hop the way every other outbound fetch in this project is.

No login is required, matching the other unauthenticated media proxies
(``PinCrisAttachmentView``, ``PinLoopnetPhotoView``): the sources reachable
here are public archival material the server already chose to publish into a
gallery, and ``services.media.media_materialize`` re-downloads these same URLs
with no session of its own.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.http import HttpResponse
from django.views import View
import requests

from urbanlens.dashboard.controllers.media_auth import mark_private_media
from urbanlens.dashboard.services.media.previews import MAX_PREVIEW_SOURCE_BYTES, render_preview, signature_is_valid
from urbanlens.dashboard.services.security.redact import redact_text
from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, fetch_public_url

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 20
_PREVIEW_CACHE_TTL = 24 * 3600
#: Cache sentinel for "this source was fetched and could not be previewed",
#: so a provider serving something unconvertible isn't re-fetched per tile
#: render. Shorter than the success TTL - a transient upstream error and a
#: genuinely unconvertible file are indistinguishable from here.
_FAILED_SENTINEL = "unpreviewable"
_FAILED_CACHE_TTL = 3600
_MAX_REDIRECTS = 5
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"


def _fetch_source(url: str) -> tuple[bytes, str] | None:
    """Download a preview source, pinning each hop to the address it validated to.

    Args:
        url: The absolute http(s) URL to fetch.

    Returns:
        ``(body, content_type)``, or None when the URL was unsafe, the fetch
        failed, or the response exceeded :data:`MAX_PREVIEW_SOURCE_BYTES`.
    """
    try:
        response = fetch_public_url(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT,
            max_redirects=_MAX_REDIRECTS,
        )
    except UnsafeUrlError:
        logger.info("Preview source rejected as unsafe: %s", redact_text(url))
        return None
    except requests.RequestException:
        logger.info("Preview source fetch failed: %s", redact_text(url))
        return None

    with response:
        if response.status_code != 200:
            return None
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_PREVIEW_SOURCE_BYTES:
                logger.info("Preview source exceeded the size cap: %s", redact_text(url))
                return None
        return bytes(body), response.headers.get("Content-Type", "")


class MediaPreviewView(View):
    """GET media-preview/?u=<url>&sig=<signature> - a web-safe render of one item.

    Serves a JPEG/PNG rendering of a TIFF/PDF/HEIC gallery item. Answers 404
    for an unsigned request, an unsafe or unreachable source, and a source
    that can't be converted - the gallery's own ``onerror`` handler then falls
    back to the icon tile, which is the correct outcome for all three.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render one signed source URL as a browser-displayable image."""
        url = request.GET.get("u", "")
        # Checked before the cache is touched or any request is made: the
        # signature is what makes this endpoint something other than an open
        # relay, so nothing may precede it.
        if not signature_is_valid(url, request.GET.get("sig", "")):
            return HttpResponse(status=404)

        cache_key = f"ul_media_preview_{hashlib.sha256(url.encode()).hexdigest()}"
        cached = cache.get(cache_key)
        if cached == _FAILED_SENTINEL:
            return HttpResponse(status=404)
        if cached is not None:
            content, content_type = cached
            return mark_private_media(HttpResponse(content, content_type=content_type))

        fetched = _fetch_source(url)
        preview = render_preview(*fetched) if fetched else None
        if preview is None:
            cache.set(cache_key, _FAILED_SENTINEL, _FAILED_CACHE_TTL)
            return HttpResponse(status=404)

        cache.set(cache_key, preview, _PREVIEW_CACHE_TTL)
        content, content_type = preview
        return mark_private_media(HttpResponse(content, content_type=content_type))
