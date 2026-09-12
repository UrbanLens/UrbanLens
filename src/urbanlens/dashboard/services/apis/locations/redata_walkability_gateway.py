"""Gateway for REData's ``/walkability/`` endpoint."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_WALKABILITY_PATH = "/api/v1/walkability/"


class RedataWalkabilityGateway(RedataLocationContextGateway):
    """REST client for REData's EPA walkability-index endpoint."""

    service_key: ClassVar[str] = "redata_walkability"

    def get_walkability(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the EPA National Walkability Index for the block group at a point.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or the request itself failed."""
        return self.near_point(_WALKABILITY_PATH, latitude, longitude, force_refresh=force_refresh)
