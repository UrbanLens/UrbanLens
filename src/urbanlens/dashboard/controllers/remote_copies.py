"""Third-party images, served from this site's own copy (``services.media.remote_copies``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.views import View

from urbanlens.dashboard.controllers.media import resolve_media_path, serve_media_file
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.media.origin import apply_media_response_headers
from urbanlens.dashboard.services.media.previews import PREVIEW_RETRY_AFTER_SECONDS, RENDER_QUEUED
from urbanlens.dashboard.services.media.remote_copies import COPY_PENDING_TTL, COPY_RATE, COPY_THROTTLE_SCOPE, pending_marker, retry_is_due
from urbanlens.dashboard.services.security import throttle
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
    page's thumbnail fallback shows an icon tile for both. Answers 503 while the first copy is being made, which
    happens on workers (``tasks.fetch_remote_image_copy``), so a slow provider holds no web request.
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
        caller = account_or_address(request)
        if not throttle.allow(COPY_THROTTLE_SCOPE, caller, COPY_RATE):
            return _not_yet(throttle.retry_after(COPY_THROTTLE_SCOPE, caller, COPY_RATE))
        if not cache.add(marker, RENDER_QUEUED, COPY_PENDING_TTL):
            return _not_yet()

        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import fetch_remote_image_copy

        if safely_enqueue_task(fetch_remote_image_copy, copy.pk, durable=False) is None:
            cache.delete(marker)
        return _not_yet()
