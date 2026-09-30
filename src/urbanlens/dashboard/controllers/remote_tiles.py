"""Map tiles from another host, served from this site's kept copies (``services.map.remote_tiles``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.views import View
import filetype

from urbanlens.dashboard.controllers.media import resolve_media_path, serve_media_file
from urbanlens.dashboard.models.remote_tiles.model import RemoteTile, RemoteTileSource
from urbanlens.dashboard.services.apis.request_upstreams import RemoteTileUpstream
from urbanlens.dashboard.services.core.gateway import SERVABLE_TILE_TYPES
from urbanlens.dashboard.services.core.request_upstream import Outcome
from urbanlens.dashboard.services.map.remote_tiles import MAX_KEPT_TILES_PER_SOURCE, coordinate_is_valid, download_tile, pending_marker, record_absent, record_failure, retry_is_due, tile_url
from urbanlens.dashboard.services.media.origin import apply_media_response_headers
from urbanlens.dashboard.services.media.previews import PREVIEW_RETRY_AFTER_SECONDS, RENDER_QUEUED, RENDER_QUEUED_TTL, discard_preview_source, stage_preview_source
from urbanlens.dashboard.services.security.throttle import account_or_address

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

#: How long a browser keeps "no tile here". A kept tile is kept for good, so its answer is immutable instead.
_ABSENT_MAX_AGE = 86400


def _not_yet(retry_after: int | None = None) -> HttpResponse:
    response = HttpResponse(status=503)
    response["Retry-After"] = str(retry_after or PREVIEW_RETRY_AFTER_SECONDS)
    response["Cache-Control"] = "no-store"
    return response


def _absent() -> HttpResponse:
    response = HttpResponse(status=404)
    response["Cache-Control"] = f"private, max-age={_ABSENT_MAX_AGE}"
    return response


class RemoteTileView(LoginRequiredMixin, View):
    """GET map/tile-copies/<digest>/<z>/<x>/<y>.png - one tile of a foreign template, from this site's copy.

    Answers 404 for a template this site never recorded, a coordinate outside the grid, a tile the host has not got,
    and a tile that failed and is not due another try. Answers 503 while the first copy is being made.
    """

    def get(self, request: HttpRequest, digest: str, z: int, x: int, y: int) -> HttpResponseBase:
        """Serve the kept tile, or start keeping it."""
        if not coordinate_is_valid(z, x, y):
            return HttpResponse(status=404)
        source = RemoteTileSource.objects.filter(template_digest=digest).first()
        if source is None:
            return HttpResponse(status=404)
        tile = RemoteTile.objects.filter(source=source, z=z, x=x, y=y).first()
        if tile is not None:
            if stored := tile.file.name:
                try:
                    response = serve_media_file(resolve_media_path(stored))
                except Http404:
                    logger.warning("Kept tile %s is missing its file", tile.pk)
                    return HttpResponse(status=404)
                response["Cache-Control"] = "private, max-age=31536000, immutable"
                return apply_media_response_headers(request, response)
            if tile.absent:
                return _absent()
        if source.kept_tiles >= MAX_KEPT_TILES_PER_SOURCE:
            return HttpResponse(status=404)

        marker = pending_marker(digest, z, x, y)
        if cache.get(marker) == RENDER_QUEUED:
            return _not_yet()
        if tile is not None and not retry_is_due(tile):
            return HttpResponse(status=404)
        if not cache.add(marker, RENDER_QUEUED, RENDER_QUEUED_TTL):
            return _not_yet()

        address = tile_url(source.template, z, x, y)
        result = RemoteTileUpstream.call(lambda: download_tile(address), caller=account_or_address(request))
        if result.outcome in (Outcome.BUSY, Outcome.THROTTLED, Outcome.TIMED_OUT):
            cache.delete(marker)
            return _not_yet(result.retry_after)
        # Only once the throttle let the fetch through, so asking for coordinates cannot grow the table on its own.
        if tile is None:
            tile, _created = RemoteTile.objects.get_or_create(source=source, z=z, x=x, y=y)
        download = result.value
        if download is not None and download.status in (204, 404):
            record_absent(tile)
            cache.delete(marker)
            return _absent()
        sniffed = filetype.guess_mime(download.body) if download is not None and download.status == 200 else None
        if download is None or sniffed not in SERVABLE_TILE_TYPES:
            record_failure(tile)
            cache.delete(marker)
            return HttpResponse(status=404)

        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import render_remote_tile

        descriptor = stage_preview_source(f"tile_{tile.pk}", download.body, sniffed)
        if safely_enqueue_task(render_remote_tile, tile.pk, descriptor, durable=False) is None:
            discard_preview_source(descriptor)
            cache.delete(marker)
        return _not_yet()
