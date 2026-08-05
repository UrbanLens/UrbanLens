"""Gateway for REData's ``/nature-observations/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /nature-observations/ - recorded
wildlife and plants". Replaces the direct, keyless call to the iNaturalist
observations API with REData's pooled view - one provider today
(``inaturalist``, worldwide).
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_NATURE_OBSERVATIONS_PATH = "/api/v1/nature-observations/"


class RedataNatureObservationsGateway(RedataLocationContextGateway):
    """REST client for REData's nearby wildlife/plant observations endpoint."""

    service_key: ClassVar[str] = "redata_nature_observations"

    def get_nearby_observations(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: float | None = None,
        quality_grade: str | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch recorded wildlife/plant observations near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters. REData defaults to 1 km
                when omitted.
            quality_grade: One of ``"research"`` (REData's default -
                community-confirmed identifications only), ``"needs_id"``, or
                ``"casual"``. Loosening it returns far more rows making a far
                weaker claim.
            limit: Maximum number of observations to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. ``coordinate_uncertainty_meters`` and
            ``attributes.obscured`` are load-bearing: providers deliberately
            obscure the location of threatened species, sometimes by tens of
            kilometres, and rendering an obscured point as precise
            misrepresents it.

        Raises:
            LocationContextUnavailableError: Every source covering the
                coordinate failed to answer, or the request to REData failed
                outright.
        """
        extra_params: dict[str, Any] = {}
        if quality_grade is not None:
            extra_params["quality_grade"] = quality_grade
        return self.near_point(
            _NATURE_OBSERVATIONS_PATH,
            latitude,
            longitude,
            radius_meters=radius_meters,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
