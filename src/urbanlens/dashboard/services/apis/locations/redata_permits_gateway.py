"""Gateway for REData's ``/permits/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /permits/ - building permits
and code violations". Providers are *cities* (Chicago, New York, San
Francisco, Austin, Seattle), each covering only its own bounding box, radius
pinned at 150 m: outside every registered city the answer is
``not_applicable``, which is a different answer from "no filings here".

``attributes.result_capped`` is set on the rows of a capped search - a dense
block fills a portal's page size, so a count of returned filings is a floor,
not a total.
"""

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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            kinds: Restrict to these ``kind`` tags (see
                :data:`PERMIT_KIND_LABELS`).
            years: How many years of filings to search.
            limit: Maximum number of filings to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope, ordered by ``issued_at`` (issued for a
            permit, cited for a violation). Entries carry ``kind``,
            ``filed_at`` (null for violations and for feeds publishing one
            date), ``estimated_cost`` (the applicant's own *declared* value -
            a scale indicator, never an appraisal), ``work_type``/``status``
            (each city's own wording, deliberately unflattened), ``address``
            as filed, and ``url`` - a deep link to the city's own record and
            the route to its plan drawings (published by Austin and Seattle;
            blank for Chicago and New York).

        Raises:
            LocationContextUnavailableError: The covering source failed to
                answer, or the request itself failed.
        """
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
