"""Proxy and catalogue for REData's basemap tile layers.

REData's tile endpoints require its API key, which must never reach the browser, so Leaflet points
at this view and the fetch happens server-side - the same arrangement ``historical_map_tiles`` uses
for warped overlay tiles.
Caching follows REData's own status contract rather than treating every response alike:

- ``200`` tiles are cached; a basemap tile is stable for a given z/x/y.
- ``404`` is definitive - the vendor confirmed no such tile, or the layer id is unknown - and is
  cached so a blank area does not re-ask on every pan.
- ``503`` means the vendor could not be reached and is never cached. Caching it would turn a vendor
  outage into a permanently blank map region, which is the sa...
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

logger = logging.getLogger(__name__)

#: Basemap tiles are stable for a given coordinate, so this is longer than the historical-map proxy's day: those
#: can change when a georeference is corrected, these change only when the vendor re-renders.
_TILE_CACHE_TTL = 7 * 86400

#: Cache sentinel for a definitive 404. Deliberately not ``b""``: a 200 whose body happens to be empty would
#: otherwise be stored as bytes identical to the sentinel and read back as "no such tile", turning a transient
#: empty answer into a permanent hole in the map.
_NO_TILE = "__ul_no_tile__"


class BasemapTileCatalogueView(LoginRequiredMixin, View):
    """GET map/basemap-tiles/sources/ - the layers this deployment can offer.

    A fallback path, not the main one: every page built on ``themes/base.html`` already carries this
    same catalogue inline (``{% basemap_tile_catalogue %}``), because a map has to know which tiles
    to draw before it draws any. This answers a client that was rendered without it.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return the layer catalogue for the signed-in viewer.

        Args:
            request: The current request.

        Returns:
            ``{"layers": [...]}`` - empty when REData is unconfigured or unreachable, so the map simply
            keeps its built-in layers.
        """
        from urbanlens.dashboard.services.map.basemap_catalogue import catalogue_for_viewer

        return JsonResponse({"layers": catalogue_for_viewer(authenticated=request.user.is_authenticated)})


class BasemapTileView(LoginRequiredMixin, View):
    """GET map/basemap-tiles/<layer>/<z>/<x>/<y>/ - one basemap tile."""

    def get(self, request: HttpRequest, layer: str, z: int, x: int, y: int) -> HttpResponse:
        """Serve one tile from cache or REData.

        Args:
            request: The current request.
            layer: Layer id from the catalogue.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            The tile bytes, a definitive 404, or an uncached 503 when the vendor could not be reached.
        """
        from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured

        if not redata_configured():
            return HttpResponse(status=404)

        cache_key = f"ul_basemap_tile_{layer}_{z}_{x}_{y}"
        cached = cache.get(cache_key)
        if cached is not None:
            if cached == _NO_TILE:
                return HttpResponse(status=404)
            # The vendor's own content type is cached with the bytes: these layers are not all PNG, and
            # mislabelling a JPEG or WebP on the cache-hit path but not the fresh one is the kind of difference
            # that shows up only once a layer is already in the cache.
            body, content_type = cached
            return HttpResponse(body, content_type=content_type)

        try:
            status, body, content_type = RedataBasemapTilesGateway().download_tile(layer, z, x, y)
        except (LocationContextUnavailableError, RequestCancelledError, OSError) as exc:
            logger.warning("Basemap tile fetch failed for %s %s/%s/%s: %s", layer, z, x, y, exc)
            return HttpResponse(status=503)

        if status == 200:
            resolved_type = content_type or "image/png"
            # Bounded like the Immich thumbnail proxy: these bytes come from a
            # vendor and land in the same shared Dragonfly that holds sessions
            # and the Channels layer - and a full store there raises rather
            # than evicting to make room, so one surprise must not turn into
            # failed cache writes for everyone sharing the store.
            # The helper also swallows a cache failure - a full or unreachable
            # Dragonfly is a degraded cache, not a broken map.
            bounded_cache.set_if_small(cache_key, body, resolved_type, _TILE_CACHE_TTL, label=f"Basemap tile {layer} {z}/{x}/{y}")
            return HttpResponse(body, content_type=resolved_type)
        if status in (400, 404):
            # A definitive answer about the request: no such tile, unknown
            # layer, or coordinates out of range. Safe to remember.
            cache.set(cache_key, _NO_TILE, _TILE_CACHE_TTL)
            return HttpResponse(status=404)
        logger.warning("Basemap tile upstream status %s for %s %s/%s/%s", status, layer, z, x, y)
        return HttpResponse(status=503)
