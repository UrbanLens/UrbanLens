"""Gateway for REData's ``/incidents/`` near-a-coordinate endpoint."""

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

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: The covering source failed to answer, the request itself failed, or a filter value was rejected."""
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
