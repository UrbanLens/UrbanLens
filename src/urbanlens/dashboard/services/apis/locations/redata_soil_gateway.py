"""Gateway for REData's ``/soil/`` endpoint."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_SOIL_PATH = "/api/v1/soil/"


class RedataSoilGateway(RedataLocationContextGateway):
    """REST client for REData's USDA soil-survey endpoint."""

    service_key: ClassVar[str] = "redata_soil"

    def get_soil_components(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the soil-survey components of the map unit at a point.

        Returns:
            The parsed envelope, dominant component first.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or the request itself failed."""
        return self.near_point(_SOIL_PATH, latitude, longitude, force_refresh=force_refresh)
