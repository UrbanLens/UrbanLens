"""Gateway for REData's ``/permits/`` near-a-coordinate endpoint."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_PERMITS_PATH = "/api/v1/permits/"

#: REData's closed ``kind`` vocabulary for this endpoint.
PERMIT_KIND_LABELS: dict[str, str] = {
    "permit": "Permit",
    "violation": "Violation",
    "site_plan": "Site plan",
}


class RedataPermitsGateway(RedataLocationContextGateway):
    """REST client for REData's building-permits/code-violations endpoint."""

    service_key: ClassVar[str] = "redata_permits"

    def get_permits(
        self,
        latitude: float,
        longitude: float,
        *,
        kinds: list[str] | None = None,
        years: int | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch permit/violation/site-plan filings near a coordinate.

        Returns:
            The parsed envelope, ordered by ``issued_at`` (issued for a permit, cited for a violation).

        Raises:
            LocationContextUnavailableError: The covering source failed to answer, or the request itself failed."""
        extra_params: dict[str, Any] = {}
        if kinds:
            extra_params["kind"] = kinds
        if years is not None:
            extra_params["years"] = years
        return self.near_point(
            _PERMITS_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
