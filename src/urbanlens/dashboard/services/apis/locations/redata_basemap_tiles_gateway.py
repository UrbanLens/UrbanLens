"""REData's basemap tile catalogue and tile bytes.

REData proxies a set of basemap layers and caches them, which buys three
things over pointing Leaflet straight at each vendor: layers this application
would otherwise have to register with each vendor itself, a single attribution
source of truth, and a cache that spares the vendor a request per pan.

The API key must never reach the browser, so the browser talks to
``controllers.basemap_tiles`` and the fetch happens server-side - the same
arrangement ``redata_historical_maps_gateway`` uses for warped overlay tiles.
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_SOURCES_PATH = "/api/v1/tiles/sources/"


class RedataBasemapTilesGateway(RedataLocationContextGateway):
    """Reads ``GET /tiles/sources/`` and ``GET /tiles/{layer}/{z}/{x}/{y}/``."""

    #: Its own key rather than the inherited one: tile traffic is one request
    #: per pan, an entirely different shape from the point lookups the base
    #: class's budget is sized for, and sharing a budget would let map panning
    #: exhaust the allowance every other location feature draws on.
    service_key: ClassVar[str] = "redata_basemap_tiles"

    def list_sources(self) -> list[dict[str, Any]]:
        """Return REData's basemap layer catalogue.

        Documented as "called once per session by whatever then requests
        tiles", so callers are expected to cache it rather than ask per map.

        Returns:
            One entry per layer, carrying ``id``, ``url_template``,
            ``attribution``, ``name``, ``min_zoom``, ``max_zoom`` and
            ``requires_auth``. Empty when REData is unconfigured or answers
            nothing.

        Raises:
            LocationContextUnavailableError: The request to REData failed.
        """
        body = self.get_json(_SOURCES_PATH, {}) or {}
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            # REData answers ``{"sources": [...]}`` for this endpoint - not the
            # ``results`` envelope its paginated collections use. Both are
            # accepted because reading the wrong one fails silently as "this
            # deployment offers no layers", which is indistinguishable from a
            # deployment that genuinely offers none.
            rows = body.get("sources") or body.get("results") or []
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict) and row.get("id")]

    def download_tile(self, layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """Fetch one basemap tile.

        Args:
            layer: Layer id from :meth:`list_sources`.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            ``(status_code, body, content_type)``. REData distinguishes a tile
            the vendor confirms does not exist (``404``) from a vendor it could
            not reach (``503``), and only the former may be cached - caching
            the latter would memorise an outage as "no map here". ``404
            unknown_layer`` and ``400 invalid_parameter`` are likewise
            definitive answers about the request itself.
        """
        base_url = (self.base_url or "").rstrip("/")
        url = f"{base_url}/api/v1/tiles/{layer}/{z}/{x}/{y}/"
        response = self.session.get(url, headers=self._headers, timeout=30)
        return response.status_code, response.content, response.headers.get("Content-Type", "image/png")
