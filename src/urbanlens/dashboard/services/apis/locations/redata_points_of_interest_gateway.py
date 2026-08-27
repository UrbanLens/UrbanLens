"""REData-backed gateway for the shared points-of-interest near-a-coordinate lookup.

REData dispatches ``GET /api/v1/points-of-interest/lookup/`` across several
registered providers behind one generic, provider-tagged envelope (see
``../REData/docs/api-reference.md``, "Points of interest"): ``nps_places``,
``osm``, ``epa_echo`` (federally regulated facilities and their compliance
history, USA only) and ``yelp`` (businesses, worldwide but key-gated on
REData's own side). This gateway backs both the ``yelp`` and ``epa_echo``
plugins - same endpoint, different ``provider=`` value - rather than each
plugin getting its own gateway class, mirroring REData's own "one registry,
one row per provider" design.

Each provider pins its own radius (``epa_echo`` fixed at 1,609 m / 1 mile,
``yelp`` fixed at 500 m) because its cache is keyed on the search anchor - a
caller-supplied ``radius_meters`` is substituted rather than honored, and
REData reports the radius actually searched per-provider on the envelope's
``providers`` block.
"""

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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            provider: One registered provider tag (e.g. ``"yelp"``,
                ``"epa_echo"``) or a list of them. Required rather than
                optional: this registry has two dozen providers and a caller
                that names none fans out across all of them, which is a
                decision worth writing down at the call site rather than
                getting by omission. A caller that genuinely wants breadth
                should ask :func:`applicable_provider_tags` which ones cover
                the point and pass that list, so the fan-out is bounded by
                coverage rather than by the whole registry.
            radius_meters: Requested search radius; most providers in this
                registry pin their own radius regardless (see the module
                docstring), so this is often a no-op honored only by
                providers that don't.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            ``PointOfInterestSerializer``-shaped dicts (``provider``,
            ``external_id``, ``name``, ``category``, ``description``,
            ``url``, ``latitude``, ``longitude``, ``attributes``,
            ``record_retrieved_at``) - possibly empty.

        Raises:
            LocationContextUnavailableError: The request failed outright or
                REData reported a transient failure (including the requested
                provider being rate-limited).
        """
        envelope = self.near_point(_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider, force_refresh=force_refresh)
        return envelope.results


#: REData's own tag for this registry's domain in ``GET /capabilities/``.
_DOMAIN_TAG = "points_of_interest"


def applicable_provider_tags(latitude: float, longitude: float) -> list[str]:
    """Which points-of-interest providers cover a coordinate, per REData.

    A thin wrapper over
    :func:`services.apis.locations.redata_capabilities_gateway.applicable_providers`
    that names this registry's domain. It matters here more than anywhere else
    in the API: most of these providers are *generated* on REData's side from
    dataset tables (one per camera register, one per FAA facility group, one
    per EPA programme), so their tags are not knowable to a client and a
    hardcoded list would silently stop growing.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The applicable provider tags, or an empty list when REData is
        unreachable or reports no coverage. Empty means "ask nothing", which is
        the safe direction *for this registry*: a request naming no provider
        fans out across all two dozen of them, so a failed discovery must not
        become that.
    """
    from urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway import applicable_providers

    return applicable_providers(_DOMAIN_TAG, latitude, longitude)
