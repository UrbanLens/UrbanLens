"""Gateway for REData's ``/air-quality/`` near-a-coordinate endpoint.
Two keyless providers answering the same question differently, returned side by side rather than reconciled:"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_AIR_QUALITY_PATH = "/api/v1/air-quality/"


class RedataAirQualityGateway(RedataLocationContextGateway):
    """REST client for REData's air-quality endpoint."""

    service_key: ClassVar[str] = "redata_air_quality"

    def get_air_quality(
        self,
        latitude: float,
        longitude: float,
        *,
        include_indoor: bool = False,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch air-quality readings near a coordinate.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: Every covering source failed to answer, or the request itself failed."""
        extra_params: dict[str, Any] = {}
        if include_indoor:
            extra_params["include_indoor"] = "true"
        return self.near_point(
            _AIR_QUALITY_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
