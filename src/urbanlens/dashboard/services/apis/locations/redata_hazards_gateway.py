"""Gateway for REData's ``/hazards/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /hazards/ - recorded
natural-hazard events". Replaces the direct, keyless call to the USGS FDSN
event catalog with REData's pooled hazards registry - one provider today
(``usgs_earthquakes``, worldwide), but the endpoint is shared across hazard
kinds (``event_type`` is a closed vocabulary: earthquake, flood, wildfire,
severe_weather, landslide, volcanic, other), so a future flood/wildfire
provider answers from the same endpoint without this gateway changing.
"""

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
        min_magnitude: float | None = None,
        years: int | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch recorded natural-hazard events near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters. REData defaults to 100 km
                (ceiling 250 km) when omitted.
            min_magnitude: Minimum event magnitude to include. Narrows the
                *fetch*, not the cache - a lower floor than a cached search
                used returns the cached set until it expires (pair with
                ``force_refresh`` when that matters).
            years: How many years back to search.
            limit: Maximum number of events to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Each ``results`` entry carries ``event_type``,
            ``magnitude``, ``magnitude_scale`` (magnitudes are not comparable
            across event types - treat the number as opaque unless you
            recognize the scale), ``occurred_at``, ``place``, ``url``, and any
            provider-specific extras under ``attributes``.

        Raises:
            LocationContextUnavailableError: Every source covering the
                coordinate failed to answer, or the request to REData failed
                outright.
        """
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
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
