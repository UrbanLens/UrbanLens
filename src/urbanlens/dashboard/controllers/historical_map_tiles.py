"""Proxy for REData's warped historical-map overlay tiles.

REData's tile pyramid (``/api/v1/maps/georeferences/{uuid}/tiles/{z}/{x}/{y}.png``) requires its API
key, which must never reach the browser - so Leaflet points at this view instead and the fetch
happens server-side.
Caching follows REData's own status contract rather than treating every response alike:

- ``200`` tiles are cached: the warp is deterministic for a given georeference.
- ``404`` is **definitive** ("no_coverage" outside the mapped area, or "not_georeferenced") and
  explicitly documented as cacheable - most of a sheet's bounding...
- ``503`` ("source_unavailable" - the institution's Image API could not be read) is never cached.
  REData deliberately serves an error rather than a blank tile ...

The bound, the size ceiling, the type allow-list and the browser's own cache are the basemap
proxy's, one route over: this is the same shape against the same upstream, and a warped tile is
slower to produce than a basemap one, not faster.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.views import View

from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, servable_tile_type
from urbanlens.dashboard.services.core.upstream_slots import UpstreamSlots as BaseUpstreamSlots
from urbanlens.UrbanLens.settings.app import settings as app_settings

logger = logging.getLogger(__name__)

#: Warped tiles are deterministic per georeference; a day keeps panning
#: cheap without holding stale tiles past a georeference correction for long.
_TILE_CACHE_TTL = 86400

#: Cache sentinel for a definitive 404. Deliberately not ``b""``: a 200 whose body happened to be
#: empty would be stored as bytes identical to the sentinel and read back as "outside the mapped
#: area", turning one empty answer into a permanent hole in the overlay.
_NO_COVERAGE = "__ul_no_coverage__"


class UpstreamSlots(BaseUpstreamSlots):
    """The process-wide bound on how many warped tiles may be fetched from REData at once."""

    @classmethod
    def limit(cls) -> int:
        """How many historical-map tile fetches one process may have in flight.

        Returns:
            ``historical_tile_upstream_concurrency``.
        """
        return app_settings.historical_tile_upstream_concurrency


def _cacheable(response: HttpResponse) -> HttpResponse:
    """Let the browser keep the answer as long as this deployment does, and not guess at its type.

    Args:
        response: The response to stamp.

    Returns:
        The same response.
    """
    # private: served behind a login, so a shared cache must not hold one. Not `immutable` like a
    # basemap tile - a georeference correction rewarps the pyramid under the same coordinates.
    response.headers["Cache-Control"] = f"private, max-age={_TILE_CACHE_TTL}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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
            The PNG tile, a definitive 404 outside the mapped area, or a 503 (uncached) when the source
            institution is unreachable or this process is already fetching as many as it may.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway
        from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

        if not redata_configured():
            return HttpResponse(status=404)

        label = f"Historical-map tile {georeference_uuid} {z}/{x}/{y}"
        cache_key = f"ul_histmap_tile_{georeference_uuid}_{z}_{x}_{y}"
        cached = bounded_cache.get_or_none(cache_key, label=label)
        if cached is not None:
            if cached == _NO_COVERAGE:
                return _cacheable(HttpResponse(status=404))
            body, content_type = cached
            return _cacheable(HttpResponse(body, content_type=content_type))

        with UpstreamSlots.hold() as slot:
            if not slot:
                # Uncached: the tile is fine, this process is already fetching as many as it is
                # allowed to. Retry-After matches the basemap proxy, whose client-side pacing
                # (`frontend/ts/shared/own-tiles.ts`) reads it.
                return HttpResponse(status=503, headers={"Retry-After": "1"})
            try:
                status, body, content_type = RedataHistoricalMapsGateway().download_tile(georeference_uuid, z, x, y)
            except (LocationContextUnavailableError, GatewayRequestError, OSError) as exc:
                logger.warning("%s fetch failed: %s", label, exc)
                return HttpResponse(status=503)
            except RequestCancelledError as exc:
                # Rate-limited or switched off: one per tile while panning, so not a warning.
                logger.debug("%s fetch refused: %s", label, exc)
                return HttpResponse(status=503)

        if status == 200:
            resolved_type = servable_tile_type(content_type)
            if resolved_type is None:
                # A fact about the upstream, not the coordinate, so it is neither cached nor given
                # a status the client will spend its retry schedule on.
                logger.warning("REData answered %s with %r, which this origin will not serve", label, content_type)
                return HttpResponse(status=404)
            # Bounded, and tolerant of a cache that cannot accept it: these bytes come from a
            # vendor, and refusing to cache must never mean refusing to answer.
            bounded_cache.set_if_small(cache_key, body, resolved_type, _TILE_CACHE_TTL, label=label)
            return _cacheable(HttpResponse(body, content_type=resolved_type))
        if status == 404:
            bounded_cache.set_or_skip(cache_key, _NO_COVERAGE, _TILE_CACHE_TTL, label=f"{label} (absent)")
            return _cacheable(HttpResponse(status=404))
        # 503 and anything unexpected: pass through uncached, so an
        # institutional outage is retried rather than memorised.
        logger.warning("%s upstream status %s", label, status)
        return HttpResponse(status=503)
