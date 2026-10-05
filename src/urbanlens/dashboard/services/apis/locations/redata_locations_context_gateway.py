"""Gateway for REData's ``GET /locations/context/``: what REData already holds for one coordinate, many domains at once.

The endpoint never fetches: a provider REData has not asked for the point reports ``not_cached`` and leaves its
domain ``complete: false``. It is charged only to the key's default pool, not the lookup pool the per-domain
endpoints share, so reading it first and asking a domain's own endpoint only for what it lacks costs less of the
budget that runs out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_SOURCE_ERROR,
    LocationContextEnvelope,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_CONTEXT_PATH = "/api/v1/locations/context/"


@dataclass(frozen=True, slots=True)
class LocationsContext:
    """REData's cached answers for one coordinate.

    Attributes:
        domains: Each returned domain's envelope, keyed by REData's domain tag (``media``, ``street_view``, ...).
        omitted: Domains REData left out because the key lacks their scope.
    """

    domains: dict[str, LocationContextEnvelope] = field(default_factory=dict)
    omitted: tuple[str, ...] = ()

    def settled(self, domain: str) -> LocationContextEnvelope | None:
        """A domain's envelope when every provider covering the point answered from cache.

        Args:
            domain: REData's domain tag.

        Returns:
            The envelope, or None when the domain is absent or any provider was ``not_cached``, down or throttled -
            then the domain's own endpoint has to be asked.
        """
        envelope = self.domains.get(domain)
        return envelope if envelope is not None and envelope.complete else None


def _envelope(body: Any) -> LocationContextEnvelope | None:
    """One domain's envelope from the context body, or None when it is not shaped like one."""
    if not isinstance(body, dict):
        return None
    results = body.get("results")
    providers = body.get("providers")
    return LocationContextEnvelope(
        count=int(body.get("count") or 0),
        complete=bool(body.get("complete", True)) and not body.get("error"),
        results=[row for row in results if isinstance(row, dict)] if isinstance(results, list) else [],
        providers=[entry for entry in providers if isinstance(entry, dict)] if isinstance(providers, list) else [],
    )


class RedataLocationsContextGateway(RedataLocationContextGateway):
    """REST client for REData's cache-only, many-domain location context read."""

    service_key: ClassVar[str] = "redata_locations_context"

    def get_context(self, latitude: float, longitude: float, domains: Sequence[str]) -> LocationsContext:
        """Read what REData holds for a coordinate in each of ``domains``.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            domains: REData domain tags to read; an empty sequence reads every domain the key may see.

        Returns:
            The parsed context.

        Raises:
            LocationContextUnavailableError: The request failed, REData rejected a domain tag, or the body was not a
                context object.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if domains:
            params["domain"] = list(domains)
        body = self.get_json(_CONTEXT_PATH, params)
        raw_domains = body.get("domains") if isinstance(body, dict) else None
        if not isinstance(raw_domains, dict):
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unexpected location context shape.")
        parsed = {tag: envelope for tag, raw in raw_domains.items() if (envelope := _envelope(raw)) is not None}
        omitted = body.get("omitted")
        return LocationsContext(domains=parsed, omitted=tuple(str(tag) for tag in omitted) if isinstance(omitted, list) else ())


__all__ = ["LocationsContext", "RedataLocationsContextGateway"]
