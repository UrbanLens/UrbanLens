"""Proxy and catalogue for REData's basemap tile layers.

REData's tile endpoints require its API key, which must never reach the
browser, so Leaflet points at this view and the fetch happens server-side -
the same arrangement ``historical_map_tiles`` uses for warped overlay tiles.

Caching follows REData's own status contract rather than treating every
response alike:

- ``200`` tiles are cached; a basemap tile is stable for a given z/x/y.
- ``404`` is definitive - the vendor confirmed no such tile, or the layer id
  is unknown - and is cached so a blank area does not re-ask on every pan.
- ``503`` means the vendor could not be reached and is never cached. Caching
  it would turn a vendor outage into a permanently blank map region, which is
  the same defect class as caching a failed panel fetch (see
  ``bin/check_outage_not_cached.py``).
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

logger = logging.getLogger(__name__)

#: Basemap tiles are stable for a given coordinate, so this is longer than the
#: historical-map proxy's day: those can change when a georeference is
#: corrected, these change only when the vendor re-renders.
_TILE_CACHE_TTL = 7 * 86400

#: The catalogue is documented as "called once per session"; a day keeps it
#: fresh enough to pick up a new layer without asking on every map load.
_CATALOGUE_CACHE_TTL = 86400
_CATALOGUE_CACHE_KEY = "ul_redata_tile_sources"

#: Cache sentinel for a definitive 404. Deliberately not ``b""``: a 200 whose
#: body happens to be empty would otherwise be stored as bytes identical to the
#: sentinel and read back as "no such tile", turning a transient empty answer
#: into a permanent hole in the map.
_NO_TILE = "__ul_no_tile__"


def _tile_url_template(layer: str) -> str:
    """Leaflet-style template for one layer, pointing at this proxy.

    Args:
        layer: The layer id.

    Returns:
        A URL with literal ``{z}``/``{x}``/``{y}`` placeholders.
    """
    from django.urls import reverse

    # Sentinel coordinates rather than braces: reverse() would encode braces as
    # %7Bz%7D, handing the client a template it cannot fill in. The values are
    # chosen not to occur in a layer id.
    concrete = reverse("map.basemap_tiles", kwargs={"layer": layer, "z": 900001, "x": 900002, "y": 900003})
    return concrete.replace("900001", "{z}").replace("900002", "{x}").replace("900003", "{y}")


class BasemapTileCatalogueView(LoginRequiredMixin, View):
    """GET map/basemap-tiles/sources/ - the layers this deployment can offer."""

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return REData's layer catalogue, rewritten to point at this proxy.

        The vendor ``url_template`` is deliberately not passed through: it
        needs REData's key, and handing the browser a template it cannot use
        would produce a layer that silently fails to load.

        Args:
            request: The current request.

        Returns:
            ``{"layers": [...]}`` - empty when REData is unconfigured or
            unreachable, so the map simply keeps its built-in layers.
        """
        from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured

        if not redata_configured():
            return JsonResponse({"layers": []})

        cached = cache.get(_CATALOGUE_CACHE_KEY)
        if cached is not None:
            return JsonResponse({"layers": cached})

        try:
            sources = RedataBasemapTilesGateway().list_sources()
        except (LocationContextUnavailableError, RequestCancelledError, OSError) as exc:
            # Not cached: an unreachable catalogue must not cost this
            # deployment its extra layers for a day.
            logger.warning("REData tile catalogue unavailable: %s", exc)
            return JsonResponse({"layers": []})

        layers = [
            {
                "id": source["id"],
                "name": source.get("name") or source["id"],
                # Attribution is not decorative - every vendor here requires it
                # on the rendered map, so a layer without it is not offered.
                "attribution": source.get("attribution") or "",
                "min_zoom": source.get("min_zoom"),
                "max_zoom": source.get("max_zoom"),
                # Reversed rather than hardcoded, with the tile coordinates
                # substituted afterwards: reverse() percent-encodes the literal
                # braces a Leaflet template needs, so they cannot be passed
                # through it.
                "url_template": _tile_url_template(source["id"]),
            }
            for source in sources
            if source.get("attribution")
        ]
        if layers:
            cache.set(_CATALOGUE_CACHE_KEY, layers, _CATALOGUE_CACHE_TTL)
        # An empty catalogue is not cached. It is indistinguishable from a
        # degraded answer - a changed envelope key, a scope the key has lost -
        # and remembering it for a day would cost this deployment every extra
        # layer until the cache expired, with nothing to indicate why.
        return JsonResponse({"layers": layers})


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
            The tile bytes, a definitive 404, or an uncached 503 when the
            vendor could not be reached.
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
            # The vendor's own content type is cached with the bytes: these
            # layers are not all PNG, and mislabelling a JPEG or WebP on the
            # cache-hit path but not the fresh one is the kind of difference
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
            cache.set(cache_key, (body, resolved_type), _TILE_CACHE_TTL)
            return HttpResponse(body, content_type=resolved_type)
        if status in (400, 404):
            # A definitive answer about the request: no such tile, unknown
            # layer, or coordinates out of range. Safe to remember.
            cache.set(cache_key, _NO_TILE, _TILE_CACHE_TTL)
            return HttpResponse(status=404)
        logger.warning("Basemap tile upstream status %s for %s %s/%s/%s", status, layer, z, x, y)
        return HttpResponse(status=503)
