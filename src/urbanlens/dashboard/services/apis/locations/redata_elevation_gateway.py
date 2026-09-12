"""Gateway for REData's ``/elevation/`` near-a-coordinate endpoint."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_ELEVATION_PATH = "/api/v1/elevation/"


class RedataElevationGateway(RedataLocationContextGateway):
    """REST client for REData's elevation-lookup endpoint."""

    service_key: ClassVar[str] = "redata_elevation"

    def get_elevation(self, latitude: float, longitude: float, *, force_refresh: bool = False) -> LocationContextEnvelope:
        """Fetch every configured DEM's elevation reading at a coordinate.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: Every configured DEM failed to answer, or the request to REData failed outright."""
        return self.near_point(_ELEVATION_PATH, latitude, longitude, force_refresh=force_refresh)
