"""An answer REData says is missing a source is shown, marked partial, and asked for again within the hour.

A partial answer cached for the whole window hides what the missing source would have added for days: a Media tab
without YouTube, a building list without Overture's footprints. Only an answer with nothing in it was guarded before.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.apis.property_records.redata_gateway import ParcelBuildings, RedataGateway
from urbanlens.dashboard.services.locations.redata_point_data import StreetViewDates
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_POINT_DATA = "urbanlens.dashboard.services.locations.redata_point_data"
_BUILDING = {"source": "cris", "name": "Main Hall", "latitude": 41.733, "longitude": -73.93}
_PARTIAL = [{"provider": "youtube", "status": "rate_limited", "count": 0}]


def _partial(rows: list[dict[str, Any]]) -> LocationContextEnvelope:
    return LocationContextEnvelope(count=len(rows), complete=False, results=rows, providers=_PARTIAL)


class _Case(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733000", longitude="-73.930000", google_place=None)
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=self.location)

    def row(self, source: str) -> LocationCache:
        return LocationCache.objects.get(location=self.location, source=source)

    def assert_partial_and_brief(self, source: str) -> None:
        row = self.row(source)
        self.assertTrue(row.data.get(UNANSWERED_SOURCES_KEY), row.data)
        self.assertIsNotNone(LocationCache.get_fresh(self.location, source))
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNone(LocationCache.get_fresh(self.location, source))


class LocationCacheTests(_Case):
    def test_a_payload_marked_partial_lapses_within_the_hour(self) -> None:
        LocationCache.set(self.location, "some_source", {"rows": [1], UNANSWERED_SOURCES_KEY: ["overture"]})

        self.assert_partial_and_brief("some_source")

    def test_an_unmarked_payload_keeps_the_window(self) -> None:
        LocationCache.set(self.location, "some_source", {"rows": [1]})

        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNotNone(LocationCache.get_fresh(self.location, "some_source"))


class ParcelBuildingsTests(_Case):
    def _fetch(self, answer: ParcelBuildings, *, osm: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from urbanlens.dashboard.plugins.builtin import parcel_buildings

        with (
            mock.patch.object(RedataGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            mock.patch.object(RedataGateway, "lookup_parcel_buildings", return_value=answer),
            mock.patch.object(parcel_buildings, "_overpass_buildings", return_value=osm or []),
            mock.patch.object(parcel_buildings, "_cris_buildings", return_value=[]),
            mock.patch.object(
                parcel_buildings, "merge_cris_buildings", side_effect=lambda found, _cris, _polygon: found
            ),
        ):
            return parcel_buildings.fetch_parcel_buildings(self.location)

    def test_an_empty_partial_answer_is_marked_rather_than_settled_as_none(self) -> None:
        payload = self._fetch(ParcelBuildings([], unanswered_sources=("overture", "overpass")))

        self.assertEqual(payload, {"buildings": [], UNANSWERED_SOURCES_KEY: ["overture", "overpass"]})

    def test_a_fallback_standing_in_for_a_partial_answer_is_marked_too(self) -> None:
        payload = self._fetch(ParcelBuildings([], unanswered_sources=("overture",)), osm=[_BUILDING])

        self.assertEqual(payload["provider"], "osm")
        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], ["overture"])

    def test_the_panel_keeps_an_empty_partial_answer_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource

        with mock.patch(
            "urbanlens.dashboard.plugins.builtin.parcel_buildings.fetch_parcel_buildings",
            return_value={"buildings": [], UNANSWERED_SOURCES_KEY: ["overture"]},
        ):
            ParcelBuildingsPanelSource().fetch(self.pin)

        self.assert_partial_and_brief(PARCEL_BUILDINGS_CACHE_SOURCE)


class BuildingAttributesTests(_Case):
    def test_a_building_read_from_a_partial_list_is_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_building_attributes import RedataBuildingAttributesPanelSource

        source = RedataBuildingAttributesPanelSource()
        LocationCache.set(
            self.location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": [_BUILDING], "provider": "redata", UNANSWERED_SOURCES_KEY: ["overture"]},
        )

        source.fetch(self.pin)

        self.assertEqual(self.row(source.cache_source).data["name"], "Main Hall")
        self.assert_partial_and_brief(source.cache_source)

    def test_a_building_read_from_a_live_partial_answer_is_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_building_attributes import RedataBuildingAttributesPanelSource

        source = RedataBuildingAttributesPanelSource()
        with (
            mock.patch.object(RedataGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            mock.patch.object(
                RedataGateway,
                "lookup_parcel_buildings",
                return_value=ParcelBuildings([_BUILDING], unanswered_sources=("overture",)),
            ),
        ):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)


class BoundaryHullTests(_Case):
    def test_a_partial_list_too_short_for_a_hull_is_deferred_not_answered(self) -> None:
        from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
        from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider

        gateway = mock.Mock()
        gateway.lookup_parcel_buildings.return_value = ParcelBuildings([_BUILDING], unanswered_sources=("overture",))

        with self.assertRaises(BoundaryProviderDeferredError):
            RedataBoundaryProvider()._buildings_convex_hull(gateway, "parcel-1", 41.733, -73.93)


class InfoPanelTests(_Case):
    def test_an_incomplete_info_panel_answer_is_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_historical_features import HistoricalFeaturesPanelSource

        source = HistoricalFeaturesPanelSource()
        with (
            mock.patch.object(source, "fetch_envelope", return_value=_partial([{"kind": "building", "name": "Mill"}])),
            mock.patch.object(source, "landed"),
        ):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)
        self.assertEqual(self.row(source.cache_source).data[UNANSWERED_SOURCES_KEY], ["youtube"])

    def test_a_partial_answer_for_a_source_kept_an_hour_is_shown_for_that_hour(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_air_quality import AirQualityPanelSource

        source = AirQualityPanelSource()
        with mock.patch.object(source, "fetch_envelope", return_value=_partial([{"parameter": "pm25", "value": 4.0}])):
            source.fetch(self.pin)

        max_age = source.cache_max_age
        self.assertIsNotNone(LocationCache.get_fresh(self.location, source.cache_source, max_age=max_age))
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNone(LocationCache.get_fresh(self.location, source.cache_source, max_age=max_age))


class MediaTests(_Case):
    _ROWS = [
        {
            "provider": "wikimedia_commons",
            "external_id": "1",
            "is_aerial": True,
            "thumbnail_url": "https://x.test/1.jpg",
        },
        {"provider": "flickr", "external_id": "2", "is_aerial": False, "thumbnail_url": "https://x.test/2.jpg"},
    ]

    def test_aerial_media_from_an_incomplete_answer_is_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_aerial_media import AerialMediaSource

        source = AerialMediaSource()
        with mock.patch(f"{_POINT_DATA}.media_near", return_value=_partial(self._ROWS)):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)

    def test_nearby_media_from_an_incomplete_answer_is_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_nearby_media import NearbyMediaSource

        source = NearbyMediaSource()
        with mock.patch(f"{_POINT_DATA}.media_near", return_value=_partial(self._ROWS)):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)

    def test_street_level_dates_from_an_incomplete_answer_are_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_street_level import StreetLevelPhotosSource

        source = StreetLevelPhotosSource()
        dates = [{"captured_on": "2020-01-01", "provider": "mapillary", "count": 1}]
        with mock.patch(f"{_POINT_DATA}.street_view_dates", return_value=StreetViewDates(dates=dates, complete=False)):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)

    def test_documents_from_an_incomplete_answer_are_kept_briefly(self) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_nearby_documents import NearbyReferenceDocumentsSource

        source = NearbyReferenceDocumentsSource()
        row = {"provider": "wikipedia", "title": "Mill", "url": "https://en.wikipedia.org/wiki/Mill"}
        with mock.patch(f"{_POINT_DATA}.reference_documents_near", return_value=_partial([row])):
            source.fetch(self.pin)

        self.assert_partial_and_brief(source.cache_source)

    def test_the_time_slider_keeps_an_incomplete_answer_briefly(self) -> None:
        from urbanlens.dashboard.services.locations.temporal_imagery import (
            REDATA_FEATURES_CACHE_SOURCE,
            fetch_redata_temporal_features,
        )

        with mock.patch(f"{_POINT_DATA}.historical_features_near", return_value=_partial([{"kind": "building"}])):
            fetch_redata_temporal_features(self.location, 41.733, -73.93)

        self.assert_partial_and_brief(REDATA_FEATURES_CACHE_SOURCE)


def _complete(rows: list[dict[str, Any]]) -> LocationContextEnvelope:
    return LocationContextEnvelope(count=len(rows), complete=True, results=rows)


class OwnFetchPanelTests(_Case):
    """Panels that make their REData calls themselves rather than through ``RedataInfoPanelSource``."""

    _GATEWAYS = "urbanlens.dashboard.services.apis.locations"

    def assert_kept_for_the_window(self, source: str) -> None:
        row = self.row(source)
        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNotNone(LocationCache.get_fresh(self.location, source))

    def _hazards(self, envelope: LocationContextEnvelope) -> str:
        from urbanlens.dashboard.plugins.builtin.hazard_history import HazardHistoryPanelSource

        source = HazardHistoryPanelSource()
        with mock.patch(
            f"{self._GATEWAYS}.redata_hazards_gateway.RedataHazardsGateway.get_hazard_events", return_value=envelope
        ):
            source.fetch(self.pin)
        return source.cache_source

    def test_fire_history_with_a_provider_unanswered_is_kept_briefly(self) -> None:
        source = self._hazards(_partial([{"provider": "nifc_wildfires", "occurred_at": "2003-08-01"}]))

        self.assert_partial_and_brief(source)
        self.assertEqual(len(self.row(source).data["events"]), 1)

    def test_complete_fire_history_keeps_the_window(self) -> None:
        self.assert_kept_for_the_window(
            self._hazards(_complete([{"provider": "nifc_wildfires", "occurred_at": "2003-08-01"}]))
        )

    def _elevation(self, envelope: LocationContextEnvelope) -> str:
        from urbanlens.dashboard.plugins.builtin.open_elevation import ElevationPanelSource

        source = ElevationPanelSource()
        with mock.patch(
            f"{self._GATEWAYS}.redata_elevation_gateway.RedataElevationGateway.get_elevation", return_value=envelope
        ):
            source.fetch(self.pin)
        return source.cache_source

    def test_a_coarser_elevation_while_a_finer_model_is_unanswered_is_kept_briefly(self) -> None:
        source = self._elevation(_partial([{"provider": "srtm", "elevation_meters": 52.0}]))

        self.assert_partial_and_brief(source)
        self.assertEqual(self.row(source).data["elevation_m"], 52.0)

    def test_a_complete_elevation_keeps_the_window(self) -> None:
        self.assert_kept_for_the_window(
            self._elevation(_complete([{"provider": "usgs_3dep", "elevation_meters": 51.0}]))
        )

    def _site_conditions(self, *, land_cover: object, walkability: object, soil: object) -> str:
        from urbanlens.dashboard.plugins.builtin.redata_site_conditions import SiteConditionsPanelSource

        def answer(outcome: object) -> dict[str, Any]:
            return {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}

        source = SiteConditionsPanelSource()
        with (
            mock.patch(
                f"{self._GATEWAYS}.redata_land_cover_gateway.RedataLandCoverGateway.get_land_cover",
                **answer(land_cover),
            ),
            mock.patch(
                f"{self._GATEWAYS}.redata_walkability_gateway.RedataWalkabilityGateway.get_walkability",
                **answer(walkability),
            ),
            mock.patch(f"{self._GATEWAYS}.redata_soil_gateway.RedataSoilGateway.get_soil_components", **answer(soil)),
        ):
            source.fetch(self.pin)
        return source.cache_source

    def test_site_conditions_with_a_domain_unreachable_are_kept_briefly(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        source = self._site_conditions(
            land_cover=_complete([{"class_name": "Developed, Low Intensity"}]),
            walkability=_complete([{"index": 9.5}]),
            soil=LocationContextUnavailableError("all_providers_unavailable", "ssurgo down"),
        )

        self.assert_partial_and_brief(source)
        self.assertEqual(self.row(source).data[UNANSWERED_SOURCES_KEY], ["soil"])
        self.assertIn("land_cover", self.row(source).data)

    def test_site_conditions_with_a_domain_redata_says_is_incomplete_are_kept_briefly(self) -> None:
        source = self._site_conditions(
            land_cover=_complete([{"class_name": "Developed, Low Intensity"}]),
            walkability=_complete([{"index": 9.5}]),
            soil=_partial([{"component_name": "Hudson"}]),
        )

        self.assert_partial_and_brief(source)

    def test_complete_site_conditions_keep_the_window(self) -> None:
        source = self._site_conditions(
            land_cover=_complete([{"class_name": "Developed, Low Intensity"}]),
            walkability=_complete([{"index": 9.5}]),
            soil=_complete([{"component_name": "Hudson"}]),
        )

        self.assert_kept_for_the_window(source)
