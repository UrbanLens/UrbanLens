"""Backend support for the beta pin/wiki "time slider": OpenHistoricalMap coverage plus per-year features.

With REData configured, both come from its ``historical-features/`` (OpenHistoricalMap's dated features, cached by
REData): one answer per point, shared with the Historical Features panel, filtered to a year here rather than asked for
again per year. Without it, the OpenHistoricalMap Overpass API is queried directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.apis.locations.open_historical_map import MAX_YEAR, MIN_YEAR, OpenHistoricalMapGateway, OpenHistoricalMapUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

#: LocationCache ``source`` for the "does OHM have coverage nearby, and what
#: years" panel. One row per Location - shared across every pin/wiki viewing it.
OHM_COVERAGE_CACHE_SOURCE = "ohm_temporal_coverage"

#: LocationCache ``source`` holding every dated feature REData traced near a Location, as GeoJSON, so each slider
#: year is a filter over one cached answer.
REDATA_FEATURES_CACHE_SOURCE = "redata_temporal_features"


def _redata_feature(row: dict[str, Any]) -> dict[str, Any] | None:
    """One REData ``HistoricalFeatureSerializer`` row as a GeoJSON Feature, or None when it has no dated bound or no shape.

    An undated feature would show in every year the slider offers, which says nothing about when it stood.
    """
    geometry = row.get("geometry")
    start, end = row.get("start_year"), row.get("end_year")
    if not isinstance(geometry, dict) or not geometry.get("type") or (start is None and end is None):
        return None
    properties = {name: row.get(name) for name in ("kind", "name", "start_year", "end_year", "source_note") if row.get(name) not in (None, "")}
    properties["id"] = f"{row.get('provider') or 'redata'}/{row.get('external_id') or row.get('uuid') or ''}"
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _year(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and MIN_YEAR <= value <= MAX_YEAR else None


def fetch_redata_temporal_features(location: Location, latitude: float, longitude: float) -> list[dict[str, Any]] | None:
    """Cache REData's dated features near a point, and the years they start and end in, for the slider.

    Args:
        location: The Location the rows belong to.
        latitude: WGS-84 latitude of the point asked about.
        longitude: WGS-84 longitude of the point asked about.

    Returns:
        The cached features, or None when no source answered, which is not cached.

    Raises:
        LocationContextUnavailableError: The request to REData failed.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.locations.redata_point_data import historical_features_near, point_key

    envelope = historical_features_near(latitude, longitude)
    if not envelope.complete and not envelope.results:
        return None
    features = [feature for row in envelope.results if (feature := _redata_feature(row)) is not None]
    years = sorted({year for feature in features for name in ("start_year", "end_year") if (year := _year(feature["properties"].get(name))) is not None})
    query_key = point_key(latitude, longitude)
    LocationCache.set(location, REDATA_FEATURES_CACHE_SOURCE, envelope.marked({"features": features}), query_key=query_key)
    LocationCache.set(location, OHM_COVERAGE_CACHE_SOURCE, envelope.marked({"available": bool(years), "years": years}), query_key=query_key)
    return features


def _standing_in(feature: dict[str, Any], year: int) -> bool:
    """Whether a feature's validity interval holds ``year``; an absent bound is open, as REData's own ``?year=`` treats it."""
    properties = feature.get("properties") or {}
    start, end = properties.get("start_year"), properties.get("end_year")
    return (start is None or start <= year) and (end is None or end >= year)


class OhmTemporalCoveragePanelSource(LocationCachePanelSource):
    """Whether OpenHistoricalMap has dated coverage near a pin's location, and for which years.
    Purely a background data source - it has no tab/template of its own (it is not an :class:`~urbanlens.dashboard.services.pins.external_data.InfoPanelSource`), so it never appears in the pin detail tab strip."""

    key = "ohm_temporal_coverage"
    cache_source = OHM_COVERAGE_CACHE_SOURCE

    # Deliberately no `required_feature` here: this source is a background-only LocationCache panel
    # (not an InfoPanelSource, no api_kinds), so it never reaches the tab strip or the external API
    # - `required_feature` would sit unread by both surfaces panel_visible_to() actually gates.
    # The real BETA_FEATURES check lives where the data is shown: temporal_slider_years() below and

    def gate(self, pin: Pin) -> bool:
        """Skip pins with no usable coordinates - mirrors ``CoordinateGatedInfoPanelSource``."""
        return bool(pin.effective_latitude and pin.effective_longitude)

    def fetch(self, pin: Pin) -> None:
        """Find dated coverage near the pin, through REData when it is configured, and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if redata_configured():
            fetch_redata_temporal_features(pin.location, float(pin.effective_latitude), float(pin.effective_longitude))
            return
        try:
            coverage = OpenHistoricalMapGateway().get_coverage(float(pin.effective_latitude), float(pin.effective_longitude))
        except OpenHistoricalMapUnavailableError:
            logger.warning("OpenHistoricalMap coverage check unavailable for pin %s", pin.pk, exc_info=True)
            return

        LocationCache.set(pin.location, OHM_COVERAGE_CACHE_SOURCE, {"available": coverage.available, "years": coverage.years})


def temporal_slider_years(location: Location | None, user: AbstractBaseUser | AnonymousUser) -> list[int]:
    """Years the beta time slider should offer for ``location``, or ``[]`` to hide it entirely.

    Args:
        location: The location being viewed, or None (e.g. a pin with no Location).
        user: The viewer, checked against :data:`SiteFeature.BETA_FEATURES`.

    Returns:
        Sorted distinct years, or ``[]`` when the viewer lacks ``BETA_FEATURES``, the coverage panel has never run (or its result has gone stale) for this location, or it ran and OHM had no dated coverage nearby."""
    if location is None or not user_has_feature(user, SiteFeature.BETA_FEATURES):
        return []

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, OHM_COVERAGE_CACHE_SOURCE)
    if row is None:
        return []
    years = row.data.get("years") or []
    return sorted(years)


def get_temporal_features(location: Location, year: int) -> dict[str, Any]:
    """A GeoJSON FeatureCollection of OHM features near ``location`` as of ``year``.

    Args:
        location: The location to query around.
        year: The calendar year to fetch features for.

    Returns:
        ``{"type": "FeatureCollection", "features": [...]}``.

    Raises:
        ValueError: ``year`` is outside the plausible ``MIN_YEAR``-``MAX_YEAR`` range."""
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}, got {year}")

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if redata_configured():
        return _redata_features_in(location, year)

    source = f"ohm_features_{year}"
    row = LocationCache.get_fresh(location, source)
    if row is not None:
        return row.data

    try:
        geojson = OpenHistoricalMapGateway().get_features_at(float(location.latitude), float(location.longitude), year)
    except OpenHistoricalMapUnavailableError:
        logger.warning("OpenHistoricalMap feature fetch unavailable for location %s, year %s", location.pk, year, exc_info=True)
        return {"type": "FeatureCollection", "features": []}

    LocationCache.set(location, source, geojson)
    return geojson


def _redata_features_in(location: Location, year: int) -> dict[str, Any]:
    """REData's cached dated features near ``location`` that stood in ``year``, fetching them once when none are cached.

    Args:
        location: The location to read around.
        year: The calendar year, already range-checked.

    Returns:
        A GeoJSON FeatureCollection; empty when REData could not be asked.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, REDATA_FEATURES_CACHE_SOURCE)
    if row is not None:
        features = (row.data or {}).get("features") or []
    else:
        try:
            features = fetch_redata_temporal_features(location, float(location.latitude), float(location.longitude)) or []
        except LocationContextUnavailableError:
            logger.warning("REData historical features unavailable for location %s", location.pk, exc_info=True)
            features = []
    return {"type": "FeatureCollection", "features": [feature for feature in features if isinstance(feature, dict) and _standing_in(feature, year)]}
