"""Gateway for REData's ``/maps/`` historical-map endpoints.
Those tiles require REData API auth, so browser-facing consumers go through UrbanLens's tile proxy (``controllers.historical_map_tiles``) rather than using REData's template directly."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_MAPS_PATH = "/api/v1/maps/"

#: Georeference sources with real control points - accurate enough to drape
#: over a modern map. ``derived_bounds`` is deliberately absent.
OVERLAY_GRADE_SOURCES = "allmaps,redata,map_warper"


class RedataHistoricalMapsGateway(RedataLocationContextGateway):
    """REST client for REData's historical-maps index."""

    service_key: ClassVar[str] = "redata_historical_maps"

    def get_maps_covering(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: float | None = None,
        covering_only: bool = False,
        kinds: str | None = None,
        overlay_grade_only: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch georeferenced historical maps covering (or near) a point.
        Reads only REData's own spatial index - never an external source - so it is cheap enough to call per page view.

        Returns:
            Match dicts ordered by containment then tightest footprint, so the first is the most detailed map of the spot.

        Raises:
            LocationContextUnavailableError: The request failed or REData rejected a parameter."""
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if radius_meters is not None:
            params["radius_meters"] = radius_meters
        if covering_only:
            params["covering_only"] = "true"
        if kinds:
            params["kind"] = kinds
        if overlay_grade_only:
            params["source"] = OVERLAY_GRADE_SOURCES
        if limit is not None:
            params["limit"] = limit
        body = self.get_json(_MAPS_PATH, params)
        return list(body.get("results") or [])

    def download_tile(self, georeference_uuid: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """Fetch one warped overlay tile from REData.

        Returns:
            ``(status_code, body, content_type)``."""
        base_url = (self.base_url or "").rstrip("/")
        url = f"{base_url}/api/v1/maps/georeferences/{georeference_uuid}/tiles/{z}/{x}/{y}.png"
        response = self.session.get(url, headers=self._headers, timeout=30)
        return response.status_code, response.content, response.headers.get("Content-Type", "image/png")
