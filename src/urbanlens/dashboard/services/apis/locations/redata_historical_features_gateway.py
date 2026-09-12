"""Gateway for REData's ``/historical-features/`` near-a-coordinate endpoint.
The source's ``start_date`` frequently means "first documented on a source of that date" (a Sanborn map, say) rather than "built in that year" - ``source_note`` names what the feature was traced from, and is what tells the two cases apart."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_HISTORICAL_FEATURES_PATH = "/api/v1/historical-features/"

#: REData's closed ``kind`` vocabulary, mapped to display labels.
#: Kept here so consumers render consistent wording without each re-deriving it, and so an
#: unrecognised kind (a future vocabulary addition) falls back visibly rather than crashing a panel.
HISTORICAL_FEATURE_KIND_LABELS: dict[str, str] = {
    "building": "Building",
    "structure": "Structure",
    "road": "Road",
    "railway": "Railway",
    "water": "Water feature",
    "landuse": "Land use",
    "place": "Place",
    "venue": "Venue",
    "other": "Other",
}


class RedataHistoricalFeaturesGateway(RedataLocationContextGateway):
    """REST client for REData's historical-features endpoint."""

    service_key: ClassVar[str] = "redata_historical_features"

    def get_historical_features(
        self,
        latitude: float,
        longitude: float,
        *,
        kinds: list[str] | None = None,
        year: int | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch mapped historical features near a coordinate.

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: The source failed to answer, the request itself failed, or a filter value was rejected."""
        extra_params: dict[str, Any] = {}
        if kinds:
            extra_params["kind"] = kinds
        if year is not None:
            extra_params["year"] = year
        return self.near_point(
            _HISTORICAL_FEATURES_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
