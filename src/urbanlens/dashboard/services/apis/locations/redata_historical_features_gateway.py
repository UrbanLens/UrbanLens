"""Gateway for REData's ``/historical-features/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /historical-features/ - mapped
features that existed near a point at some time": buildings, roads, railways,
water features, land use, places and named venues retrospectively traced from
historical sources (OpenHistoricalMap today), most of them long since gone.

Two contract points that shape any consumer:

- ``start_year`` is **not** a construction year. The source's ``start_date``
  frequently means "first documented on a source of that date" (a Sanborn
  map, say) rather than "built in that year" - ``source_note`` names what the
  feature was traced from, and is what tells the two cases apart. Neither
  bound should be presented to an end user as an age.
- Volunteer-traced and city-scale coverage: an empty result means "nothing
  mapped here", never "nothing was here".
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_HISTORICAL_FEATURES_PATH = "/api/v1/historical-features/"

#: REData's closed ``kind`` vocabulary, mapped to display labels. Kept here so
#: consumers render consistent wording without each re-deriving it, and so an
#: unrecognised kind (a future vocabulary addition) falls back visibly rather
#: than crashing a panel.
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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            kinds: Restrict to these ``kind`` tags (see
                :data:`HISTORICAL_FEATURE_KIND_LABELS`). An unknown kind is a
                REData ``400``, surfaced as
                :class:`LocationContextUnavailableError`.
            year: Only features whose validity interval contains this year.
                Applied to REData's cached result, not the fetch, so
                narrowing never prunes the cached set a later, different year
                needs. A null ``start_year``/``end_year`` counts as matching
                any year - see the module docstring.
            limit: Maximum number of features to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Each ``results`` entry carries ``kind``,
            ``name``, ``start_year``/``end_year`` (nullable), the publisher's
            raw ``start_date``/``end_date`` strings, ``source_note`` (what the
            feature was traced from), and real GeoJSON ``geometry``.

        Raises:
            LocationContextUnavailableError: The source failed to answer, the
                request itself failed, or a filter value was rejected.
        """
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
