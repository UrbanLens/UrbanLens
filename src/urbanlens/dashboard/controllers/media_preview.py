"""Server-side preview for gallery items browsers cannot render."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.http import HttpResponse
from django.views import View

from urbanlens.dashboard.controllers.media_auth import mark_private_media
from urbanlens.dashboard.services.media.previews import MAX_PREVIEW_SOURCE_BYTES, UNPREVIEWABLE, cached_preview, fetch_remote_source, request_sandbox_render, signature_is_valid, stage_preview_source, unfinished_preview_response

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_PREVIEW_CACHE_TTL = 24 * 3600
#: Shorter than the success TTL - a transient upstream error and a genuinely unconvertible file are
#: indistinguishable from here. The sentinel value itself is shared with the render task, which is what writes
#: it now.
_FAILED_CACHE_TTL = 3600
#: How long the staged source's descriptor stays cached. Longer than RENDER_QUEUED_TTL on purpose: when that
#: marker expires and the next request re-queues, the source is still on disk and does not have to be
#: re-downloaded.
_SOURCE_CACHE_TTL = 1800


class MediaPreviewView(View):
    """GET media-preview/?u=<url>&sig=<signature> - a web-safe render of one item.

    Serves a JPEG/PNG rendering of a TIFF/PDF/HEIC gallery item.
    Answers 404 for an unsigned request, an unsafe or unreachable source, and a source that can't be
    converted - the gallery's own ``onerror`` handler then falls back to the icon tile, which is the
    correct outcome for all three.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render one signed source URL as a browser-displayable image."""
        url = request.GET.get("u", "")
        # Checked before the cache is touched or any request is made: the signature is what makes this endpoint
        # something other than an open relay, so nothing may precede it.
        if not signature_is_valid(url, request.GET.get("sig", "")):
            return HttpResponse(status=404)

        digest = hashlib.sha256(url.encode()).hexdigest()
        cache_key = f"ul_media_preview_{digest}"
        if (preview := cached_preview(cache_key)) is not None:
            content, content_type = preview
            return mark_private_media(HttpResponse(content, content_type=content_type))
        if cache.get(cache_key) is not None:
            # Unconvertible or already queued; neither should re-fetch the source.
            return unfinished_preview_response(cache_key)

        # A source staged by an earlier request that has not been rendered yet - its RENDER_QUEUED marker
        # expired, so this request re-queues it.
        source_key = f"ul_media_preview_src_{digest}"
        if cache.get(source_key) is None:
            fetched = fetch_remote_source(url, max_bytes=MAX_PREVIEW_SOURCE_BYTES)
            if fetched is None:
                cache.set(cache_key, UNPREVIEWABLE, _FAILED_CACHE_TTL)
                return HttpResponse(status=404)
            # The fetch stays here - a network call, not a decode, made by the process holding the signed URL.
            cache.set(source_key, stage_preview_source(digest, *fetched), _SOURCE_CACHE_TTL)

        request_sandbox_render(source_key, cache_key, ttl=_PREVIEW_CACHE_TTL, failure_ttl=_FAILED_CACHE_TTL)
        return unfinished_preview_response(cache_key)
