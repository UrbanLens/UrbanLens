"""Proxy for REData's warped historical-map overlay tiles.

REData's tile pyramid (``/api/v1/maps/georeferences/{uuid}/tiles/{z}/{x}/{y}.png``)
requires its API key, which must never reach the browser - so Leaflet points
at this view instead and the fetch happens server-side.

Caching follows REData's own status contract rather than treating every
response alike:

- ``200`` tiles are cached: the warp is deterministic for a given
  georeference.
- ``404`` is **definitive** ("no_coverage" outside the mapped area, or
  "not_georeferenced") and explicitly documented as cacheable - most of a
  sheet's bounding pyramid is outside its mask, so caching the misses matters
  as much as caching the hits.
- ``503`` ("source_unavailable" - the institution's Image API could not be
  read) is never cached. REData deliberately serves an error rather than a
  blank tile so that an outage can't be memorised as "no map here"; caching
  it here would defeat exactly that.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.views import View

logger = logging.getLogger(__name__)

#: Warped tiles are deterministic per georeference; a day keeps panning
#: cheap without holding stale tiles past a georeference correction for long.
_TILE_CACHE_TTL = 86400

#: Cache sentinel for a definitive 404 - distinct from "not cached".
_NO_COVERAGE = b""


class HistoricalMapTileView(LoginRequiredMixin, View):
    """GET map/historical-tiles/<georeference_uuid>/<z>/<x>/<y>.png - one warped overlay tile."""

    def get(self, request: HttpRequest, georeference_uuid: str, z: int, x: int, y: int) -> HttpResponse:
        """Serve one tile from cache or REData.

        Args:
            request: The current request.
            georeference_uuid: REData georeference whose pyramid to read.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            The PNG tile, a definitive 404 outside the mapped area, or a 503
            (uncached) when the source institution is unreachable.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway

        if not redata_configured():
            return HttpResponse(status=404)

        cache_key = f"ul_histmap_tile_{georeference_uuid}_{z}_{x}_{y}"
        cached = cache.get(cache_key)
        if cached is not None:
            if cached == _NO_COVERAGE:
                return HttpResponse(status=404)
            return HttpResponse(cached, content_type="image/png")

        try:
            status, body, content_type = RedataHistoricalMapsGateway().download_tile(georeference_uuid, z, x, y)
        except (LocationContextUnavailableError, OSError) as exc:
            logger.warning("Historical-map tile fetch failed for %s %s/%s/%s: %s", georeference_uuid, z, x, y, exc)
            return HttpResponse(status=503)

        if status == 200:
            cache.set(cache_key, body, _TILE_CACHE_TTL)
            return HttpResponse(body, content_type=content_type or "image/png")
        if status == 404:
            cache.set(cache_key, _NO_COVERAGE, _TILE_CACHE_TTL)
            return HttpResponse(status=404)
        # 503 and anything unexpected: pass through uncached, so an
        # institutional outage is retried rather than memorised.
        logger.warning("Historical-map tile upstream status %s for %s %s/%s/%s", status, georeference_uuid, z, x, y)
        return HttpResponse(status=503)
