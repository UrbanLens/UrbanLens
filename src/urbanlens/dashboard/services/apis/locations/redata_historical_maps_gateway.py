"""Gateway for REData's ``/maps/`` historical-map endpoints.
Those tiles require REData API auth, so browser-facing consumers go through UrbanLens's tile proxy (``controllers.historical_map_tiles``) rather than using REData's template directly."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway
from urbanlens.dashboard.services.core.gateway import read_capped

_MAPS_PATH = "/api/v1/maps/"

#: Georeference authors accurate enough to list. ``derived_bounds`` is approximate, and it is
#: also how Library of Congress Sanborn sheets are placed - LoC publishes no control points.
OVERLAY_GRADE_SOURCES = "allmaps,redata,map_warper,derived_bounds"


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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: How far beyond the point to accept a nearby sheet
                (REData default 1000, max 50000).
            covering_only: Require the point to fall inside the map's
                footprint.
            kinds: Comma-separated sheet kinds (``fire_insurance``,
                ``cadastral``, ``topographic``, ``panoramic``, ``nautical``,
                ``other``).
            overlay_grade_only: Restrict to georeferences with real control
                points (see :data:`OVERLAY_GRADE_SOURCES`). Off, approximate
                ``derived_bounds`` placements are included too.
            limit: Maximum matches (REData default 25, max 200).

        Returns:
            Match dicts ordered by containment then tightest footprint, so the first is the most detailed map of the spot.

        Raises:
            LocationContextUnavailableError: The request failed or REData rejected a parameter.
        """
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

        Args:
            georeference_uuid: The georeference whose tile pyramid to read.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            ``(status_code, body, content_type)``.
        """
        base_url = (self.base_url or "").rstrip("/")
        url = f"{base_url}/api/v1/maps/georeferences/{georeference_uuid}/tiles/{z}/{x}/{y}.png"
        response = self.session.get(url, headers=self._headers, timeout=30, stream=True)
        return response.status_code, read_capped(response, what="historical map tile"), response.headers.get("Content-Type", "image/png")
