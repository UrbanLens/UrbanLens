"""Gateway for REData's ``/maps/`` historical-map endpoints.

See ``../REData/docs/api-reference.md``, "Historical maps": scanned fire
insurance plans, cadastral atlases and panoramic views indexed by *where
they sit on the earth*, with community georeferences (Allmaps, Map Warper,
REData's own) stored as real polygons. Distinct from ``/imagery/`` ("what
pictures exist of this place") - this answers "what maps were drawn of it,
and where exactly do they sit".

Each match pairs a ``sheet`` (the scan and its catalogue record) with its
preferred ``georeference``, whose ``tile_url_template`` serves warped
``{z}/{x}/{y}.png`` overlay tiles. Those tiles require REData API auth, so
browser-facing consumers go through UrbanLens's tile proxy
(``controllers.historical_map_tiles``) rather than using REData's template
directly.

``source=allmaps,redata,map_warper`` filters out ``derived_bounds``
placements - corner-derived affines good enough for coverage queries but not
overlay-grade (they assume a north-up scan cropped exactly to its map area).
"""

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

        Reads only REData's own spatial index - never an external source - so
        it is cheap enough to call per page view.

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
            Match dicts ordered by containment then tightest footprint, so
            the first is the most detailed map of the spot. Each carries
            ``sheet`` (title, ``date_text``/``year_start``/``year_end``,
            ``kind``, ``attribution``), ``georeference`` (``uuid``,
            ``tile_url_template``, ``bounds`` as
            ``[min_lon, min_lat, max_lon, max_lat]``, ``rmse_meters``),
            ``contains_point`` and ``distance_meters``.

        Raises:
            LocationContextUnavailableError: The request failed or REData
                rejected a parameter.
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
            ``(status_code, body, content_type)``. ``200`` carries a PNG with
            transparency outside the map's mask. ``404`` is definitive
            ("no_coverage" outside the mapped area, or "not_georeferenced")
            and safe to cache; ``503`` means the institution's Image API
            could not be read and must NOT be cached - REData deliberately
            never serves a blank tile in its place.
        """
        base_url = (self.base_url or "").rstrip("/")
        url = f"{base_url}/api/v1/maps/georeferences/{georeference_uuid}/tiles/{z}/{x}/{y}.png"
        response = self.session.get(url, headers=self._headers, timeout=30)
        return response.status_code, response.content, response.headers.get("Content-Type", "image/png")
