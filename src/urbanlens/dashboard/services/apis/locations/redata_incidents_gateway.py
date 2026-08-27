"""Gateway for REData's ``/incidents/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /incidents/ - reported police
incidents". Providers are *cities* (nine municipal open-data portals), radius
pinned at 500 m (block scale) for every one; outside every registered city
the answer is ``not_applicable``, which is different from "nothing happened
here".

Contract points any consumer must respect:

- ``location_precision``: every publisher fuzzes location before release
  (block centroid / nearest intersection / hundred block). A point is NOT
  evidence about a specific building.
- ``arrest_made`` is nullable and null is not false - only two of the nine
  cities publish it.
- ``attributes.completeness_lag_days``, where present, marks a recent window
  the publisher itself says is incomplete.
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_INCIDENTS_PATH = "/api/v1/incidents/"

#: REData's closed ``category`` vocabulary, mapped to display labels.
#: ``traffic`` is a road collision rather than a crime, and only some feeds
#: publish any - filter it out before comparing counts across places.
INCIDENT_CATEGORY_LABELS: dict[str, str] = {
    "theft": "Theft",
    "robbery": "Robbery",
    "burglary": "Burglary",
    "vehicle": "Vehicle crime",
    "assault": "Assault",
    "homicide": "Homicide",
    "sex_offense": "Sex offense",
    "weapons": "Weapons",
    "narcotics": "Narcotics",
    "vandalism": "Vandalism",
    "fraud": "Fraud",
    "disorder": "Disorder",
    "traffic": "Traffic collision",
    "other": "Other",
}


class RedataIncidentsGateway(RedataLocationContextGateway):
    """REST client for REData's reported-police-incidents endpoint."""

    service_key: ClassVar[str] = "redata_incidents"

    def get_incidents(
        self,
        latitude: float,
        longitude: float,
        *,
        categories: list[str] | None = None,
        years: int | None = None,
        arrests_only: bool = False,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch reported police incidents near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            categories: Restrict to these ``category`` tags (see
                :data:`INCIDENT_CATEGORY_LABELS`). Applied to the result
                only, so narrowing never prunes REData's cached set.
            years: How many years back to search (REData default 3, max 25).
                Bounds the fetch as well as the result.
            arrests_only: Only incidents with a published arrest. Returns
                nothing for the seven cities that publish no arrest flag -
                deliberately, because treating silence as "no arrest" would
                manufacture a statistic.
            limit: Maximum number of incidents to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Entries carry ``category``,
            ``offense_description``, ``occurred_at``/``reported_at`` (real
            instants resolved in the city's own zone; the two routinely
            differ by days), ``location_precision``, nullable
            ``arrest_made``/``domestic``, and the publisher's own
            class/wording plus collapse bookkeeping (``offenses``,
            ``source_row_count``) under ``attributes``.

        Raises:
            LocationContextUnavailableError: The covering source failed to
                answer, the request itself failed, or a filter value was
                rejected.
        """
        extra_params: dict[str, Any] = {}
        if categories:
            extra_params["category"] = categories
        if years is not None:
            extra_params["years"] = years
        if arrests_only:
            extra_params["arrests_only"] = "true"
        return self.near_point(
            _INCIDENTS_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
