"""Gateway for REData's ``/capabilities/`` discovery endpoint."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway
from urbanlens.dashboard.services.security.redact import redact_coordinate

logger = logging.getLogger(__name__)

_CAPABILITIES_PATH = "/api/v1/capabilities/"

#: How long a point's provider list is cached. It changes on REData deploys,
#: not per request, and reading it costs REData no external call - but it does
#: cost a round trip, and a panel fetch should not pay for one per pin.
_CAPABILITIES_TTL_SECONDS = 60 * 60


class RedataCapabilitiesGateway(RedataLocationContextGateway):
    """REST client for REData's capability index."""

    service_key: ClassVar[str] = "redata_capabilities"

    def get_capabilities(self, *, latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
        """Fetch the capability index, optionally scoped to a point.

        Returns:
            ``{"domains": [...], "text_domains": [...]}``.

        Raises:
            LocationContextUnavailableError: The request failed."""
        params: dict[str, Any] = {}
        if latitude is not None and longitude is not None:
            params = {"lat": latitude, "lng": longitude}
        return self.get_json(_CAPABILITIES_PATH, params)


def applicable_providers(domain_tag: str, latitude: float, longitude: float) -> list[str]:
    """Which of a domain's providers cover a coordinate, per REData.
    A client that names its own list stops growing the day REData registers a source, silently, which is the failure this exists to prevent.

    Args:
        domain_tag: REData's own tag for the domain, e.g. ``"imagery"`` or ``"points_of_interest"``.
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The applicable provider tags, or an empty list when REData is unreachable or reports no coverage. **Empty is ambiguous on purpose** and each caller has to decide what it means for them: for a registry where a provider-less request fans out across..."""
    from django.core.cache import cache

    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

    key = f"redata_providers:{domain_tag}:{latitude:.2f},{longitude:.2f}"
    cached = cache.get(key)
    if cached is not None:
        return list(cached)

    try:
        index = RedataCapabilitiesGateway().get_capabilities(latitude=latitude, longitude=longitude)
    except LocationContextUnavailableError as exc:
        logger.info(
            "REData capability index unavailable for %s at %s,%s: %s",
            domain_tag,
            redact_coordinate(latitude),
            redact_coordinate(longitude),
            exc.reason,
        )
        return []

    tags: list[str] = []
    for domain in index.get("domains") or []:
        if isinstance(domain, dict) and domain.get("tag") == domain_tag:
            tags = [tag for tag in (domain.get("applicable_providers") or []) if isinstance(tag, str)]
            break
    cache.set(key, tags, _CAPABILITIES_TTL_SECONDS)
    return tags
