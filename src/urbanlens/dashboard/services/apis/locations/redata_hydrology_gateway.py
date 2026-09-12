"""Gateway for REData's ``/hydrology/`` near-a-coordinate endpoint. All USA-only and keyless."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_HYDROLOGY_PATH = "/api/v1/hydrology/"

#: codes; the source's own code survives in ``feature_type``).
HYDROLOGY_KIND_LABELS: dict[str, str] = {
    "stream": "Stream",
    "waterbody": "Waterbody",
    "wetland": "Wetland",
    "watershed": "Watershed",
}


class RedataHydrologyGateway(RedataLocationContextGateway):
    """REST client for REData's hydrology endpoint."""

    service_key: ClassVar[str] = "redata_hydrology"

    def get_hydrology(
        self,
        latitude: float,
        longitude: float,
        *,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch streams, waterbodies, wetlands and the containing watershed.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: Every covering source failed to answer, or the request itself failed."""
        return self.near_point(
            _HYDROLOGY_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
        )
