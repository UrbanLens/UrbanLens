"""
Backend support for the beta pin/wiki "time slider": OpenHistoricalMap coverage plus per-year features.

Where a :class:`TemporalFeatureProvider` is available, both come from it: one answer per point, filtered to a year
here rather than asked for again per year. Without one, the OpenHistoricalMap Overpass API is queried directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.apis.locations.open_historical_map import MAX_YEAR, MIN_YEAR, OpenHistoricalMapGateway, OpenHistoricalMapUnavailableError
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError
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


class TemporalFeatureProvider(ABC):
    """A source of dated map features that answers for the time slider in place of OpenHistoricalMap's public API."""

    @abstractmethod
    def available(self) -> bool:
        """Whether this install can ask the source."""

    @abstractmethod
    def prefetch(self, location: Location, latitude: float, longitude: float) -> None:
        """
        Cache the dated features near a point and the coverage row (``OHM_COVERAGE_CACHE_SOURCE``) the slider reads.

        Raises:
            ProviderUnavailableError: The source could not be asked.
        """

    @abstractmethod
    def features_in(self, location: Location, year: int) -> dict[str, Any]:
        """
        A GeoJSON FeatureCollection of the features near ``location`` that stood in ``year``, fetching them once when none are cached.

        Returns:
            The collection; empty when the source could not be asked.
        """


def active_temporal_provider() -> TemporalFeatureProvider | None:
    """The first available temporal feature provider, or None where the public API is used."""
    from urbanlens.dashboard.plugins.points import TEMPORAL_FEATURE_PROVIDERS

    return next((provider for provider in TEMPORAL_FEATURE_PROVIDERS if provider.available()), None)


class OhmTemporalCoveragePanelSource(LocationCachePanelSource):
    """
    Whether OpenHistoricalMap has dated coverage near a pin's location, and for which years.
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
        """Find dated coverage near the pin, through the temporal feature provider when there is one, and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if (provider := active_temporal_provider()) is not None:
            provider.prefetch(pin.location, float(pin.effective_latitude), float(pin.effective_longitude))
            return
        try:
            coverage = OpenHistoricalMapGateway().get_coverage(float(pin.effective_latitude), float(pin.effective_longitude))
        except OpenHistoricalMapUnavailableError:
            logger.warning("OpenHistoricalMap coverage check unavailable for pin %s", pin.pk, exc_info=True)
            return

        LocationCache.set(pin.location, OHM_COVERAGE_CACHE_SOURCE, {"available": coverage.available, "years": coverage.years})


def temporal_slider_years(location: Location | None, user: AbstractBaseUser | AnonymousUser) -> list[int]:
    """
    Years the beta time slider should offer for ``location``, or ``[]`` to hide it entirely.

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
    """
    A GeoJSON FeatureCollection of OHM features near ``location`` as of ``year``.

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

    if (provider := active_temporal_provider()) is not None:
        return provider.features_in(location, year)

    source = f"ohm_features_{year}"
    row = LocationCache.get_fresh(location, source)
    if row is not None:
        return row.data

    try:
        geojson = OpenHistoricalMapGateway().get_features_at(float(location.latitude), float(location.longitude), year)
    except ImpossibleInputError:
        # Counted where it was refused; OHM has nothing at a point it cannot be asked about.
        return {"type": "FeatureCollection", "features": []}
    except OpenHistoricalMapUnavailableError:
        logger.warning("OpenHistoricalMap feature fetch unavailable for location %s, year %s", location.pk, year, exc_info=True)
        return {"type": "FeatureCollection", "features": []}

    LocationCache.set(location, source, geojson)
    return geojson
