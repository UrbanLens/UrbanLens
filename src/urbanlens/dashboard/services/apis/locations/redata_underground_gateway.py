"""Gateway for REData's ``/underground/`` near-a-coordinate endpoint."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_UNDERGROUND_PATH = "/api/v1/underground/"

#: REData's closed ``kind`` vocabulary, mapped to display labels.
#: Kept here so consumers render consistent wording without each re-deriving it, and so an
#: unrecognised kind (a future vocabulary addition) falls back visibly rather than crashing a panel.
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

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: The source failed to answer, the request itself failed, or a filter value was rejected."""
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
