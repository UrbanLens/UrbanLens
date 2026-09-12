"""REData-backed gateway for the shared points-of-interest near-a-coordinate lookup."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

logger = logging.getLogger(__name__)

_PATH = "/api/v1/points-of-interest/lookup/"


@dataclass(slots=True, kw_only=True)
class RedataPointsOfInterestGateway(RedataLocationContextGateway):
    """REST client for REData's ``/points-of-interest/lookup/`` near-a-coordinate search."""

    service_key: ClassVar[str] = "redata_points_of_interest"

    def find_near(self, latitude: float, longitude: float, *, provider: str | list[str], radius_meters: float | None = None, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return points of interest from named REData providers near a coordinate.

        Returns:
            ``PointOfInterestSerializer``-shaped dicts (``provider``, ``external_id``, ``name``, ``category``, ``description``, ``url``, ``latitude``, ``longitude``, ``attributes``, ``record_retrieved_at``) - possibly empty.

        Raises:
            LocationContextUnavailableError: The request failed outright or REData reported a transient failure (including the requested provider being rate-limited)."""
        envelope = self.near_point(_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider, force_refresh=force_refresh)
        return envelope.results


#: REData's own tag for this registry's domain in ``GET /capabilities/``.
_DOMAIN_TAG = "points_of_interest"


def applicable_provider_tags(latitude: float, longitude: float) -> list[str]:
    """Which points-of-interest providers cover a coordinate, per REData.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The applicable provider tags, or an empty list when REData is unreachable or reports no coverage."""
    from urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway import applicable_providers

    return applicable_providers(_DOMAIN_TAG, latitude, longitude)
