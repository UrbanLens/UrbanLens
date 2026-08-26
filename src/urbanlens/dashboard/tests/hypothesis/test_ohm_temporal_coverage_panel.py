"""Tests for services.locations.temporal_imagery: the beta time-slider's backend.

Covers the coverage panel's gate()/fetch() (mirroring
test_usgs_earthquakes_panel.py's style), temporal_slider_years()'s visibility
decision, and get_temporal_features()'s per-year caching - including the
cross-year isolation the module's own docstring calls out as the detail most
worth getting right.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.open_historical_map import OhmCoverage, OpenHistoricalMapUnavailableError
from urbanlens.dashboard.services.locations.temporal_imagery import (
    OHM_COVERAGE_CACHE_SOURCE,
    OhmTemporalCoveragePanelSource,
    get_temporal_features,
    temporal_slider_years,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class OhmTemporalCoveragePanelSourceGateTests(TestCase):
    """gate() requires the pin to have usable coordinates."""

    def setUp(self) -> None:
        super().setUp()
        self.source = OhmTemporalCoveragePanelSource()

    def test_gate_true_with_coordinates(self) -> None:
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        self.assertTrue(self.source.gate(pin))

    def test_gate_false_without_coordinates(self) -> None:
        location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        self.assertFalse(self.source.gate(pin))


class OhmTemporalCoveragePanelSourceFetchTests(TestCase):
    """fetch() persists (or withholds) a LocationCache row depending on the gateway's outcome."""

    def setUp(self) -> None:
        super().setUp()
        self.source = OhmTemporalCoveragePanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def test_fetch_caches_available_coverage(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_coverage.return_value = OhmCoverage(available=True, years=[1900, 1950])
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE)
        assert cached is not None
        self.assertEqual(cached.data, {"available": True, "years": [1900, 1950]})
        mock_gateway_cls.return_value.get_coverage.assert_called_once_with(40.5, -74.5)

    def test_fetch_caches_an_explicit_empty_result(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_coverage.return_value = OhmCoverage(available=False, years=[])
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE)
        assert cached is not None
        self.assertEqual(cached.data, {"available": False, "years": []})

    def test_fetch_does_not_cache_on_transient_failure(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_coverage.side_effect = OpenHistoricalMapUnavailableError("boom")
            self.source.fetch(self.pin)

        self.assertIsNone(LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE))


class TemporalSliderYearsTests(TestCase):
    """temporal_slider_years() decides whether the beta slider should render at all."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.location: Location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)

    def test_empty_without_the_beta_feature(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.location, OHM_COVERAGE_CACHE_SOURCE, {"available": True, "years": [1950, 1900]})
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.user_has_feature", return_value=False):
            self.assertEqual(temporal_slider_years(self.location, self.user), [])

    def test_empty_when_no_coverage_row_exists(self) -> None:
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.user_has_feature", return_value=True):
            self.assertEqual(temporal_slider_years(self.location, self.user), [])

    def test_empty_when_coverage_row_has_no_years(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.location, OHM_COVERAGE_CACHE_SOURCE, {"available": False, "years": []})
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.user_has_feature", return_value=True):
            self.assertEqual(temporal_slider_years(self.location, self.user), [])

    def test_returns_sorted_years_when_gated_and_covered(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.location, OHM_COVERAGE_CACHE_SOURCE, {"available": True, "years": [1950, 1900, 2020]})
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.user_has_feature", return_value=True):
            self.assertEqual(temporal_slider_years(self.location, self.user), [1900, 1950, 2020])

    def test_empty_when_location_is_none(self) -> None:
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.user_has_feature", return_value=True):
            self.assertEqual(temporal_slider_years(None, self.user), [])


class GetTemporalFeaturesTests(TestCase):
    """get_temporal_features() validates the year and caches results per-year."""

    def setUp(self) -> None:
        super().setUp()
        self.location: Location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)

    def test_rejects_year_out_of_bounds(self) -> None:
        with pytest.raises(ValueError, match="year"):
            get_temporal_features(self.location, 999)
        with pytest.raises(ValueError, match="year"):
            get_temporal_features(self.location, 2101)

    def test_fetches_and_caches_on_a_cold_year(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        geojson = {"type": "FeatureCollection", "features": [{"type": "Feature"}]}
        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_features_at.return_value = geojson
            result = get_temporal_features(self.location, 1950)

        self.assertEqual(result, geojson)
        cached = LocationCache.get_fresh(self.location, "ohm_features_1950")
        assert cached is not None
        self.assertEqual(cached.data, geojson)
        mock_gateway_cls.return_value.get_features_at.assert_called_once_with(40.5, -74.5, 1950)

    def test_reads_from_cache_without_calling_the_gateway(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cached_geojson = {"type": "FeatureCollection", "features": []}
        LocationCache.set(self.location, "ohm_features_1950", cached_geojson)

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            result = get_temporal_features(self.location, 1950)

        self.assertEqual(result, cached_geojson)
        mock_gateway_cls.return_value.get_features_at.assert_not_called()

    def test_transient_failure_returns_empty_collection_without_caching(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_features_at.side_effect = OpenHistoricalMapUnavailableError("boom")
            result = get_temporal_features(self.location, 1950)

        self.assertEqual(result, {"type": "FeatureCollection", "features": []})
        self.assertIsNone(LocationCache.get_fresh(self.location, "ohm_features_1950"))

    def test_different_years_are_cached_independently(self) -> None:
        """The bug this module's docstring warns about: one shared source string would let years clobber each other."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        geojson_1900 = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"id": "node/1900"}}]}
        geojson_2000 = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"id": "node/2000"}}]}

        with mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_features_at.return_value = geojson_1900
            get_temporal_features(self.location, 1900)
            mock_gateway_cls.return_value.get_features_at.return_value = geojson_2000
            get_temporal_features(self.location, 2000)

        cached_1900 = LocationCache.get_fresh(self.location, "ohm_features_1900")
        cached_2000 = LocationCache.get_fresh(self.location, "ohm_features_2000")
        assert cached_1900 is not None
        assert cached_2000 is not None
        self.assertEqual(cached_1900.data, geojson_1900)
        self.assertEqual(cached_2000.data, geojson_2000)
