"""REData-backed gateway for its public-locations catalog (state capitols, county seats, national capitals).

Backs the demo instance's location pool (``services.demo.locations``), not any
user-facing panel - ``GET /api/v1/public-locations/`` answers from REData's own
local catalog with no per-source attribution (``{"count","results"}``, no
``providers``/``complete`` block), which is why this does not go through
:meth:`RedataLocationContextGateway.near_point` the way every other gateway in
this package does: ``near_point`` always sends ``lat``/``lng``, but this
endpoint's whole point for the demo is browsing the catalog *without* a
coordinate - see REData's ``docs/api-reference.md``, "Public locations".

As of 2026-08-20 this endpoint exists on REData's own working tree but is not
yet deployed anywhere UrbanLens can reach. Every caller here is written to
degrade to an empty list rather than raise when it 404s or the configured key
lacks the ``public_locations:read`` scope, since "REData doesn't have this yet"
and "REData is unreachable" must both leave demo seeding with no pins - not
with a stack trace - see ``services.demo.locations.pool_locations``' own
"empty pool is correct" precedent.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, RedataLocationContextGateway

logger = logging.getLogger(__name__)

_PATH = "/api/v1/public-locations/"

#: REData's own enum (parcels.models.public_location.meta.PublicLocationKind).
#: Duplicated here rather than fetched, matching how this project already
#: treats REData enums elsewhere (e.g. redata_historic_registers.py) - it is
#: REData's contract to keep stable, not a value this project can discover any
#: other way, and a value outside this set is REData's own 400 to raise.
PUBLIC_LOCATION_KINDS = ("state_capitol", "county_seat", "national_capital")


@dataclass(slots=True, kw_only=True)
class RedataPublicLocationsGateway(RedataLocationContextGateway):
    """REST client for REData's ``/public-locations/`` local catalog."""

    service_key: ClassVar[str] = "redata_public_locations"

    def list_public_locations(self, *, kind: str | None = None, country: str | None = None, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List catalog entries, optionally filtered - no coordinate required.

        Args:
            kind: One of :data:`PUBLIC_LOCATION_KINDS`, or None for every kind.
            country: ISO 3166-1 alpha-2, case-insensitive.
            state: USPS state abbreviation, case-insensitive (only
                ``state_capitol``/``county_seat`` rows carry one).
            limit: Bounded positive integer; REData defaults to 50 and caps at
                200 for this endpoint.

        Returns:
            ``PublicLocationSerializer``-shaped dicts (``uuid``, ``kind``,
            ``name``, ``country``, ``state``, ``county_name``, ``latitude``,
            ``longitude``, ``source``, ``catalog_synced_at``) - possibly
            empty, including when REData does not have this endpoint yet.
        """
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        if country is not None:
            params["country"] = country
        if state is not None:
            params["state"] = state

        try:
            body = self.get_json(_PATH, params)
        except LocationContextUnavailableError:
            logger.exception("redata_public_locations: request failed, treating as empty")
            return []
        if not isinstance(body, dict):
            return []
        results = body.get("results")
        return results if isinstance(results, list) else []
