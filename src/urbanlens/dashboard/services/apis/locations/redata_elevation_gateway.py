"""Gateway for REData's ``/elevation/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /elevation/ - metres above sea
level". Replaces the direct, keyless call to the public Open-Elevation
instance (SRTM-derived, ~90 m resolution, no per-source disagreement to show)
with REData's pooled view across every digital elevation model it has
configured (USGS 3DEP, Open-Elevation, Open-Meteo/Copernicus).
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_ELEVATION_PATH = "/api/v1/elevation/"


class RedataElevationGateway(RedataLocationContextGateway):
    """REST client for REData's elevation-lookup endpoint."""

    service_key: ClassVar[str] = "redata_elevation"

    def get_elevation(self, latitude: float, longitude: float, *, force_refresh: bool = False) -> LocationContextEnvelope:
        """Fetch every configured DEM's elevation reading at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Each ``results`` entry is
            ``{"provider", "dataset", "resolution_meters", "elevation_meters",
            "status"}``, in REData's recommended order (finest resolution
            first where it applies) - never a single winner, since digital
            elevation models genuinely disagree with each other by real
            margins on the same terrain. ``elevation_meters: null`` alongside
            ``status: "ok"`` is itself a real answer (a gap in that model's
            coverage, e.g. open ocean), not a failure.

        Raises:
            LocationContextUnavailableError: Every configured DEM failed to
                answer, or the request to REData failed outright.
        """
        return self.near_point(_ELEVATION_PATH, latitude, longitude, force_refresh=force_refresh)
