"""Gateway for REData's ``/land-cover/`` endpoint.
One value covers a 30 m raster pixel (``resolution_meters``): a small urban parcel sits inside a single pixel, so the answer describes its block rather than its garden."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_LAND_COVER_PATH = "/api/v1/land-cover/"


class RedataLandCoverGateway(RedataLocationContextGateway):
    """REST client for REData's NLCD land-cover endpoint."""

    service_key: ClassVar[str] = "redata_land_cover"

    def get_land_cover(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the NLCD land-cover classification at a point.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or the request itself failed."""
        return self.near_point(_LAND_COVER_PATH, latitude, longitude, force_refresh=force_refresh)
