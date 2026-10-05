"""The beta time slider read from REData's ``historical-features/`` when REData is configured, instead of OHM directly."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.plugins.builtin.redata_historical_features import HistoricalFeaturesPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)
from urbanlens.dashboard.services.apis.locations.redata_historical_features_gateway import (
    RedataHistoricalFeaturesGateway,
)
from urbanlens.dashboard.services.locations import redata_point_data
from urbanlens.dashboard.services.locations.temporal_imagery import (
    OHM_COVERAGE_CACHE_SOURCE,
    REDATA_FEATURES_CACHE_SOURCE,
    OhmTemporalCoveragePanelSource,
    get_temporal_features,
)
from urbanlens.UrbanLens.settings.app import settings

_SQUARE = {"type": "Polygon", "coordinates": [[[-74.5, 40.5], [-74.4, 40.5], [-74.4, 40.6], [-74.5, 40.5]]]}


def _feature(name: str, start: int | None, end: int | None, **overrides: object) -> dict:
    """A row in REData's ``HistoricalFeatureSerializer`` shape."""
    row = {
        "uuid": f"0000000{len(name)}-0000-4000-8000-000000000000",
        "provider": "openhistoricalmap",
        "external_id": f"way/{name}",
        "kind": "building",
        "name": name,
        "start_year": start,
        "end_year": end,
        "start_date": str(start or ""),
        "end_date": str(end or ""),
        "source_note": "1909 Sanborn",
        "geometry": _SQUARE,
        "latitude": 40.55,
        "longitude": -74.45,
        "attributes": {},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


ROWS = [
    _feature("Mill", 1890, 1962),
    _feature("Pier", None, 1925),
    _feature("Warehouse", 1950, None),
    _feature("Undated shed", None, None),
    _feature("No shape", 1900, 1910, geometry=None),
]


class _RedataSliderCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(settings, "redata_api_url", "https://redata.example.test"))
        self.enterContext(mock.patch.object(settings, "redata_api_key", "k"))
        self.enterContext(mock.patch.object(redata_point_data, "settled_domain", return_value=None))
        self.ohm = self.enterContext(
            mock.patch("urbanlens.dashboard.services.locations.temporal_imagery.OpenHistoricalMapGateway")
        )
        self.location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def answer(self, rows: list[dict], *, complete: bool = True) -> mock.Mock:
        envelope = LocationContextEnvelope(count=len(rows), complete=complete, results=rows)
        return self.enterContext(
            mock.patch.object(RedataHistoricalFeaturesGateway, "get_historical_features", return_value=envelope)
        )


class RedataSliderCoverageTests(_RedataSliderCase):
    def test_coverage_years_are_the_dated_features_bounds(self) -> None:
        self.answer(ROWS)
        OhmTemporalCoveragePanelSource().fetch(self.pin)

        coverage = LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE)
        features = LocationCache.get_fresh(self.location, REDATA_FEATURES_CACHE_SOURCE)
        assert coverage is not None and features is not None
        self.assertEqual(coverage.data, {"available": True, "years": [1890, 1925, 1950, 1962]})
        names = [feature["properties"]["name"] for feature in features.data["features"]]
        self.assertEqual(names, ["Mill", "Pier", "Warehouse"])
        self.ohm.assert_not_called()

    def test_an_outage_caches_nothing(self) -> None:
        self.answer([], complete=False)
        OhmTemporalCoveragePanelSource().fetch(self.pin)
        self.assertIsNone(LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE))

    def test_the_historical_features_panel_fills_the_slider_from_its_own_answer(self) -> None:
        """The panel is fetched on every pin page; the slider's coverage rides on it rather than asking again."""
        get_historical_features = self.answer(ROWS)
        HistoricalFeaturesPanelSource().fetch(self.pin)

        get_historical_features.assert_called_once()
        coverage = LocationCache.get_fresh(self.location, OHM_COVERAGE_CACHE_SOURCE)
        assert coverage is not None
        self.assertEqual(coverage.data["years"], [1890, 1925, 1950, 1962])
        panel = LocationCache.get_fresh(self.location, "redata_historical_features")
        assert panel is not None
        self.assertNotIn("geometry", panel.data["features"][0])


class RedataSliderYearTests(_RedataSliderCase):
    def _names(self, year: int) -> list[str]:
        return [feature["properties"]["name"] for feature in get_temporal_features(self.location, year)["features"]]

    def test_a_year_is_a_filter_over_one_cached_answer(self) -> None:
        get_historical_features = self.answer(ROWS)
        self.assertEqual(self._names(1900), ["Mill", "Pier"])
        self.assertEqual(self._names(1955), ["Mill", "Warehouse"])
        self.assertEqual(self._names(1970), ["Warehouse"])
        get_historical_features.assert_called_once()
        self.ohm.assert_not_called()

    def test_an_unreachable_redata_answers_an_empty_year_and_caches_nothing(self) -> None:
        self.enterContext(
            mock.patch.object(
                RedataHistoricalFeaturesGateway,
                "get_historical_features",
                side_effect=LocationContextUnavailableError("source_error", "down"),
            )
        )
        self.assertEqual(get_temporal_features(self.location, 1900), {"type": "FeatureCollection", "features": []})
        self.assertIsNone(LocationCache.get_fresh(self.location, REDATA_FEATURES_CACHE_SOURCE))
