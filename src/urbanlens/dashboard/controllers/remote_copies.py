"""Third-party images, served from this site's own copy (``services.media.remote_copies``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.views import View

from urbanlens.dashboard.controllers.media import resolve_media_path, serve_media_file
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.apis.request_upstreams import RemoteImageCopyUpstream
from urbanlens.dashboard.services.core.request_upstream import Outcome
from urbanlens.dashboard.services.media.origin import apply_media_response_headers
from urbanlens.dashboard.services.media.previews import PREVIEW_RETRY_AFTER_SECONDS, RENDER_QUEUED, RENDER_QUEUED_TTL, discard_preview_source, fetch_remote_source, stage_preview_source
from urbanlens.dashboard.services.media.remote_copies import MAX_REMOTE_COPY_SOURCE_BYTES, pending_marker, record_failure, retry_is_due
from urbanlens.dashboard.services.security.throttle import account_or_address

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)


def _not_yet(retry_after: int | None = None) -> HttpResponse:
    response = HttpResponse(status=503)
    response["Retry-After"] = str(retry_after or PREVIEW_RETRY_AFTER_SECONDS)
    response["Cache-Control"] = "no-store"
    return response


class RemoteImageCopyView(View):
    """GET media-copy/<digest>/ - one third-party image, from this site's copy.

    Answers 404 for a digest this site never issued, and for a source that failed and is not due another try; the
    page's thumbnail fallback shows an icon tile for both. Answers 503 while the first copy is being made.
    """

    def get(self, request: HttpRequest, digest: str) -> HttpResponseBase:
        """Serve the stored copy, or start making it."""
        copy = RemoteImageCopy.objects.filter(url_digest=digest).first()
        if copy is None:
            return HttpResponse(status=404)
        if stored := copy.file.name:
            try:
                response = serve_media_file(resolve_media_path(stored))
            except Http404:
                logger.warning("Stored copy %s is missing its file", copy.pk)
                return HttpResponse(status=404)
            response["Cache-Control"] = "public, max-age=31536000, immutable"
            return apply_media_response_headers(request, response)

        marker = pending_marker(digest)
        if cache.get(marker) == RENDER_QUEUED:
            return _not_yet()
        if not retry_is_due(copy):
            return HttpResponse(status=404)
        if not cache.add(marker, RENDER_QUEUED, RENDER_QUEUED_TTL):
            return _not_yet()

        source_url = copy.source_url
        result = RemoteImageCopyUpstream.call(lambda: fetch_remote_source(source_url, max_bytes=MAX_REMOTE_COPY_SOURCE_BYTES), caller=account_or_address(request))
        if result.outcome in (Outcome.BUSY, Outcome.THROTTLED, Outcome.TIMED_OUT):
            cache.delete(marker)
            return _not_yet(result.retry_after)
        fetched = result.value
        if fetched is None:
            record_failure(copy)
            cache.delete(marker)
            return HttpResponse(status=404)

        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import render_remote_image_copy

        descriptor = stage_preview_source(f"copy_{digest}", *fetched)
        if safely_enqueue_task(render_remote_image_copy, copy.pk, descriptor, durable=False) is None:
            discard_preview_source(descriptor)
            cache.delete(marker)
        return _not_yet()
