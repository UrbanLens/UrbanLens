"""REData's basemap tile catalogue and tile bytes."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_SOURCES_PATH = "/api/v1/tiles/sources/"


class RedataBasemapTilesGateway(RedataLocationContextGateway):
    """Reads ``GET /tiles/sources/`` and ``GET /tiles/{layer}/{z}/{x}/{y}/``."""

    #: Its own key rather than the inherited one: tile traffic is one request per pan, an entirely
    #: different shape from the point lookups the base class's budget is sized for, and sharing a
    #: budget would let map panning exhaust the allowance every other location feature draws on.
    service_key: ClassVar[str] = "redata_basemap_tiles"

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """Record the layer, never the tile coordinate.

        Returns:
            The URL truncated at the layer segment."""
        marker = "/api/v1/tiles/"
        if marker not in url:
            return url
        prefix, _, rest = url.partition(marker)
        layer = rest.split("/", 1)[0]
        return f"{prefix}{marker}{layer}/" if layer else f"{prefix}{marker}"

    def list_sources(self) -> list[dict[str, Any]]:
        """Return REData's basemap layer catalogue.
        Documented as "called once per session by whatever then requests tiles", so callers are expected to cache it rather than ask per map.

        Returns:
            One entry per layer, carrying ``id``, ``url_template``, ``attribution``, ``name``, ``min_zoom``, ``max_zoom`` and ``requires_auth``.

        Raises:
            LocationContextUnavailableError: The request to REData failed."""
        body = self.get_json(_SOURCES_PATH, {}) or {}
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            # REData answers ``{"sources": [...]}`` for this endpoint - not the ``results`` envelope
            # its paginated collections use.
            # Both are accepted because reading the wrong one fails silently as "this deployment
            # offers no layers", which is indistinguishable from a deployment that genuinely offers
            rows = body.get("sources") or body.get("results") or []
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict) and row.get("id")]

    def download_tile(self, layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """Fetch one basemap tile.

        Returns:
            ``(status_code, body, content_type)``."""
        base_url = (self.base_url or "").rstrip("/")
        url = f"{base_url}/api/v1/tiles/{layer}/{z}/{x}/{y}/"
        response = self.session.get(url, headers=self._headers, timeout=30)
        return response.status_code, response.content, response.headers.get("Content-Type", "image/png")
