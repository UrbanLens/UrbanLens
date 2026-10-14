"""The beta time slider asks a temporal feature provider where there is one, and OpenHistoricalMap's public API otherwise."""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.plugins.points import TEMPORAL_FEATURE_PROVIDERS
from urbanlens.dashboard.services.locations.temporal_imagery import (
    OHM_COVERAGE_CACHE_SOURCE,
    OhmTemporalCoveragePanelSource,
    TemporalFeatureProvider,
    active_temporal_provider,
    get_temporal_features,
)

_OHM = "urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway"
_FEATURES = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "geometry": None, "properties": {"name": "Mill"}}],
}


class _Provider(TemporalFeatureProvider):
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.prefetched: list[tuple[float, float]] = []
        self.asked: list[int] = []

    def available(self) -> bool:
        return self._available

    def prefetch(self, location: Any, latitude: float, longitude: float) -> None:
        self.prefetched.append((latitude, longitude))

    def features_in(self, location: Any, year: int) -> dict[str, Any]:
        self.asked.append(year)
        return _FEATURES


class TemporalFeatureProviderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def test_the_first_available_provider_is_the_one_asked(self) -> None:
        unavailable, first, second = _Provider(available=False), _Provider(), _Provider()

        with TEMPORAL_FEATURE_PROVIDERS.overridden([unavailable, first, second]):
            self.assertIs(active_temporal_provider(), first)

    def test_with_no_provider_there_is_none_to_ask(self) -> None:
        with TEMPORAL_FEATURE_PROVIDERS.overridden([_Provider(available=False)]):
            self.assertIsNone(active_temporal_provider())

    def test_a_years_features_come_from_the_provider_and_not_from_ohm(self) -> None:
        provider = _Provider()

        with TEMPORAL_FEATURE_PROVIDERS.overridden([provider]), mock.patch(_OHM) as ohm:
            result = get_temporal_features(self.location, 1900)

        self.assertEqual(result, _FEATURES)
        self.assertEqual(provider.asked, [1900])
        ohm.assert_not_called()

    def test_the_coverage_panel_asks_the_provider_to_cache_the_pins_point(self) -> None:
        provider = _Provider()

        with TEMPORAL_FEATURE_PROVIDERS.overridden([provider]), mock.patch(_OHM) as ohm:
            OhmTemporalCoveragePanelSource().fetch(self.pin)

        self.assertEqual(
            provider.prefetched, [(float(self.pin.effective_latitude), float(self.pin.effective_longitude))]
        )
        ohm.assert_not_called()
        self.assertFalse(
            LocationCache.objects.filter(location=self.location, source=OHM_COVERAGE_CACHE_SOURCE).exists(),
            "the provider writes the coverage row, not core",
        )

    def test_without_a_provider_ohm_is_asked(self) -> None:
        with TEMPORAL_FEATURE_PROVIDERS.overridden([]), mock.patch(_OHM) as ohm:
            ohm.return_value.get_features_at.return_value = _FEATURES
            result = get_temporal_features(self.location, 1900)

        self.assertEqual(result, _FEATURES)
        ohm.assert_called_once()
