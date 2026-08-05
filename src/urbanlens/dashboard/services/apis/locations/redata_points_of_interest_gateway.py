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
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_PATH = "/api/v1/points-of-interest/lookup/"


@dataclass(slots=True, kw_only=True)
class RedataPointsOfInterestGateway(RedataLocationContextGateway):
    """REST client for REData's ``/points-of-interest/lookup/`` near-a-coordinate search."""

    service_key: ClassVar[str] = "redata_points_of_interest"

    def find_near(self, latitude: float, longitude: float, *, provider: str, radius_meters: float | None = None, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return points of interest from one REData provider near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            provider: Restrict to a single registered provider tag (e.g.
                ``"yelp"``, ``"epa_echo"``) - required rather than optional so
                a caller never accidentally fans out across every registered
                provider (including ones it has no code to interpret).
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
