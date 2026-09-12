"""REData-backed gateway for its public-locations catalog (state capitols, county seats, national capitals)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, RedataLocationContextGateway

logger = logging.getLogger(__name__)

_PATH = "/api/v1/public-locations/"

#: REData's own enum (parcels.models.public_location.meta.PublicLocationKind).
#: Duplicated here rather than fetched, matching how this project already treats REData enums
#: elsewhere (e.g. redata_historic_registers.py) - it is REData's contract to keep stable, not a
#: value this project can discover any other way, and a value outside this set is REData's own 400
PUBLIC_LOCATION_KINDS = ("state_capitol", "county_seat", "national_capital")


@dataclass(slots=True, kw_only=True)
class RedataPublicLocationsGateway(RedataLocationContextGateway):
    """REST client for REData's ``/public-locations/`` local catalog."""

    service_key: ClassVar[str] = "redata_public_locations"

    def list_public_locations(self, *, kind: str | None = None, country: str | None = None, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List catalog entries, optionally filtered - no coordinate required.

        Returns:
            ``PublicLocationSerializer``-shaped dicts (``uuid``, ``kind``, ``name``, ``country``, ``state``, ``county_name``, ``latitude``, ``longitude``, ``source``, ``catalog_synced_at``) - possibly empty, including when REData does not have this endpoint yet."""
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
