"""Gateway for REData's ``/underground/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /underground/ - mapped
subsurface structures": tunnels, station levels, pedestrian passages,
culverts, buried utility runs and access shafts, from OpenStreetMap
(worldwide, keyless, radius pinned at 250 m server-side).

Two contract points that shape any consumer:

- ``geometry`` is the answer, not the coordinate. A tunnel is a LineString
  and OSM splits routes into segments of arbitrary length, so a segment's
  ``latitude``/``longitude`` (a representative point for marker placement)
  can sit hundreds of metres from where it actually crosses a site.
- Volunteer-mapped and far from complete: an empty result means "nothing
  mapped here", never "nothing there".
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_UNDERGROUND_PATH = "/api/v1/underground/"

#: REData's closed ``kind`` vocabulary, mapped to display labels. Kept here so
#: consumers render consistent wording without each re-deriving it, and so an
#: unrecognised kind (a future vocabulary addition) falls back visibly rather
#: than crashing a panel.
UNDERGROUND_KIND_LABELS: dict[str, str] = {
    "rail_tunnel": "Rail tunnel",
    "road_tunnel": "Road tunnel",
    "pedestrian_passage": "Pedestrian passage",
    "station_level": "Underground station level",
    "water_conduit": "Culvert / buried stream",
    "utility_line": "Buried utility line",
    "pipeline": "Pipeline",
    "access_point": "Access point (manhole/shaft/vent)",
    "service_tunnel": "Service tunnel",
    "chamber": "Underground chamber",
    "other": "Underground structure",
}


class RedataUndergroundGateway(RedataLocationContextGateway):
    """REST client for REData's subsurface-structures endpoint."""

    service_key: ClassVar[str] = "redata_underground"

    def get_underground_structures(
        self,
        latitude: float,
        longitude: float,
        *,
        kinds: list[str] | None = None,
        enterable_only: bool = False,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch mapped subsurface structures near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            kinds: Restrict to these ``kind`` tags (see
                :data:`UNDERGROUND_KIND_LABELS`). An unknown kind is a REData
                ``400``, surfaced as :class:`LocationContextUnavailableError`.
            enterable_only: Only structures a person could be inside
                (REData's derived ``is_enterable``).
            limit: Maximum number of structures to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Each ``results`` entry carries ``kind``,
            ``name``, ``is_enterable``, ``layer`` (OSM stacking order - NOT a
            depth in metres), real GeoJSON ``geometry``, and OSM tag extras
            (including ``disused:``/``abandoned:`` provenance) under
            ``attributes``.

        Raises:
            LocationContextUnavailableError: The source failed to answer, the
                request itself failed, or a filter value was rejected.
        """
        extra_params: dict[str, Any] = {}
        if kinds:
            extra_params["kind"] = kinds
        if enterable_only:
            extra_params["enterable_only"] = "true"
        return self.near_point(
            _UNDERGROUND_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
