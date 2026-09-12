"""REData-backed gateway for the cultural/historic-resource near-a-coordinate lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_PATH = "/api/v1/cultural-resources/lookup/"

#: REData's own tag for this registry's domain in ``GET /capabilities/``.
DOMAIN_TAG = "cultural_resources"


@dataclass(slots=True, kw_only=True)
class RedataCulturalResourcesGateway(RedataLocationContextGateway):
    """REST client for REData's ``/cultural-resources/lookup/`` near-a-coordinate search."""

    service_key: ClassVar[str] = "redata_cultural_resources"

    def near_resources(self, latitude: float, longitude: float, *, provider: str | list[str], radius_meters: float | None = None) -> LocationContextEnvelope:
        """Return historic-register records near a coordinate, with the envelope intact.

        Returns:
            The ``{count, complete, results, providers}`` envelope.

        Raises:
            LocationContextUnavailableError: The request failed outright, or REData reported a transient failure."""
        return self.near_point(_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider)


def applicable_provider_tags(latitude: float, longitude: float) -> list[str]:
    """Which historic-register providers cover a coordinate, per REData.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The applicable provider tags, or an empty list when REData is unreachable or reports no coverage."""
    from urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway import applicable_providers

    return applicable_providers(DOMAIN_TAG, latitude, longitude)
