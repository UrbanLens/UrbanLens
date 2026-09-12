"""Gateway for REData's ``/hazards/`` near-a-coordinate endpoint."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_HAZARDS_PATH = "/api/v1/hazards/"


class RedataHazardsGateway(RedataLocationContextGateway):
    """REST client for REData's natural-hazard-events endpoint."""

    service_key: ClassVar[str] = "redata_hazards"

    def get_hazard_events(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: float | None = None,
        providers: list[str] | None = None,
        min_magnitude: float | None = None,
        years: int | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch recorded natural-hazard events near a coordinate.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: Every source covering the coordinate failed to answer, or the request to REData failed outright."""
        extra_params: dict[str, Any] = {}
        if min_magnitude is not None:
            extra_params["min_magnitude"] = min_magnitude
        if years is not None:
            extra_params["years"] = years
        return self.near_point(
            _HAZARDS_PATH,
            latitude,
            longitude,
            radius_meters=radius_meters,
            provider=providers,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
