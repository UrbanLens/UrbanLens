"""An answer REData says is missing a source is shown, marked partial, and asked for again within the hour.

A partial answer cached for the whole window hides what the missing source would have added for days: a Media tab
without YouTube, a building list without Overture's footprints. Only an answer with nothing in it was guarded before.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
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


_RECORD_GATEWAY = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"
_APN = "12-34-567"
_ASSESSMENT = {"parcel_identifier": _APN, "tax_year": 2025, "total_value": "250000", "value_stage": "final"}
_TIER_UNANSWERED = [
    {"tier": 1, "status": "unavailable", "host": "gis.example.gov", "http_status": 503, "message": "down"}
]


def _outage() -> Exception:
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
        REASON_SOURCE_ERROR,
        PropertyRecordsUnavailableError,
    )

    return PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "upstream timed out", retry_later=True)


class PropertyRecordTests(_Case):
    """The sections asked for beside the parcel record: liens, tax, owners, sales, assessments, demographics, parks.

    The record itself was already kept briefly when REData answered it in part. A section REData could not answer was
    left out and the record cached for the whole window, so the liens or tax history it lacked stayed hidden for days.
    """

    def _gateway(self, gateway_cls: mock.Mock) -> mock.Mock:
        gateway = gateway_cls.return_value
        gateway.lookup_parcel.return_value = {"uuid": "parcel-1", "apn": _APN}
        gateway.lookup_coverage.return_value = {}
        gateway.lookup_assessments.return_value = _complete([_ASSESSMENT])
        gateway.lookup_sale_records.return_value = _complete([])
        gateway.lookup_liens.return_value = [
            {"lien_type": "code", "amount": "500", "filed_date": "2024-01-02", "status": "open"}
        ]
        gateway.lookup_tax_payments.return_value = [{"tax_year": 2025, "paid": False, "delinquent": True}]
        gateway.lookup_owners.return_value = []
        gateway.lookup_sales.return_value = []
        gateway.lookup_demographics.return_value = {"population": 1000}
        gateway.lookup_national_parks.return_value = {}
        return gateway

    def _cached(self, **lookups: object) -> dict[str, Any]:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        with mock.patch(_RECORD_GATEWAY) as gateway_cls:
            gateway = self._gateway(gateway_cls)
            for name, answer in lookups.items():
                lookup = getattr(gateway, f"lookup_{name}")
                if isinstance(answer, Exception):
                    lookup.side_effect = answer
                else:
                    lookup.return_value = answer
            PropertyRecordsPanelSource().fetch(self.pin)
        return self.row("property_records").data

    def test_a_section_redata_could_not_answer_is_asked_for_again_within_the_hour(self) -> None:
        data = self._cached(liens=_outage())

        self.assertIn("liens", data[UNANSWERED_SOURCES_KEY])
        self.assertNotIn("liens", {key for key in data if key != UNANSWERED_SOURCES_KEY})
        self.assertTrue(data["available"])
        self.assertIn("tax_status", data, "the sections that did come back are still shown")
        self.assertIn("assessment_history", data)
        self.assert_partial_and_brief("property_records")

    def test_every_section_redata_could_not_answer_is_named(self) -> None:
        data = self._cached(
            tax_payments=_outage(), owners=_outage(), sales=_outage(), national_parks=_outage(), demographics=_outage()
        )

        self.assertEqual(
            sorted(data[UNANSWERED_SOURCES_KEY]), ["demographics", "national-parks", "owners", "sales", "tax-payments"]
        )
        self.assertIn("liens", data)

    def test_a_section_redata_refused_for_good_keeps_the_window(self) -> None:
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_FORBIDDEN,
            PropertyRecordsUnavailableError,
        )

        self._cached(liens=PropertyRecordsUnavailableError(REASON_FORBIDDEN, "This key may not read liens."))

        self.assert_kept_for_the_window("property_records")

    def test_demographics_redata_is_not_configured_for_keeps_the_window(self) -> None:
        """REData without a Census key refuses every parcel alike, and asking again within the hour changes nothing."""
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

        refusal = PropertyRecordsUnavailableError("census_data_api_not_configured", "No Census key.", retry_later=True)
        self._cached(demographics=refusal)

        self.assert_kept_for_the_window("property_records")

    def test_an_assessment_answer_missing_a_provider_keeps_its_rows_briefly(self) -> None:
        partial = LocationContextEnvelope(
            count=1, complete=False, results=[_ASSESSMENT], providers=[{"provider": "ny_orps", "status": "unavailable"}]
        )

        data = self._cached(assessments=partial)

        self.assertEqual([row["tax_year"] for row in data["assessment_history"]], [2025])
        self.assertIn("assessments:ny_orps", data[UNANSWERED_SOURCES_KEY])
        self.assert_partial_and_brief("property_records")

    def test_a_sale_record_answer_missing_a_provider_is_asked_for_again(self) -> None:
        partial = LocationContextEnvelope(
            count=0, complete=False, results=[], providers=[{"provider": "ct_opm", "status": "rate_limited"}]
        )

        data = self._cached(sale_records=partial)

        self.assertIn("sale-records:ct_opm", data[UNANSWERED_SOURCES_KEY])
        self.assert_partial_and_brief("property_records")

    def test_a_complete_record_keeps_the_window(self) -> None:
        data = self._cached()

        self.assertNotIn(UNANSWERED_SOURCES_KEY, data)
        self.assert_kept_for_the_window("property_records")

    def assert_kept_for_the_window(self, source: str) -> None:
        row = self.row(source)
        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNotNone(LocationCache.get_fresh(self.location, source))


class ProviderResultsGatewayTests(SimpleTestCase):
    """``/assessments/`` and ``/sale-records/`` answer REData's provider envelope; its ``complete`` was dropped."""

    def _answer(self, read: str, body: dict[str, Any]) -> LocationContextEnvelope:
        session = mock.Mock()
        response = mock.Mock(status_code=200, headers={}, text="")
        response.json.return_value = body
        session.get.return_value = response
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)
        answer = getattr(gateway, read)("parcel-1")
        assert isinstance(answer, LocationContextEnvelope)
        return answer

    def test_an_answer_missing_a_provider_says_which(self) -> None:
        body = {
            "count": 1,
            "complete": False,
            "results": [_ASSESSMENT],
            "providers": [{"provider": "ny_orps", "status": "unavailable"}, {"provider": "ok", "status": "ok"}],
        }
        for read in ("lookup_assessments", "lookup_sale_records"):
            with self.subTest(read=read):
                answer = self._answer(read, body)

                self.assertEqual((answer.results, answer.unanswered_sources), ([_ASSESSMENT], ["ny_orps"]))

    def test_an_older_redata_that_sends_no_complete_is_taken_as_complete(self) -> None:
        for read in ("lookup_assessments", "lookup_sale_records"):
            with self.subTest(read=read):
                answer = self._answer(read, {"results": [_ASSESSMENT]})

                self.assertEqual((answer.results, answer.complete), ([_ASSESSMENT], True))


class OfficialOwnerTests(_Case):
    """A record REData answered in part may name today's owner; it is not evidence that anyone else stopped owning."""

    def _linked(self) -> set[str]:
        return set(self.location.owners.values_list("name", flat=True))

    def test_a_partial_record_links_its_owner_and_unlinks_nobody(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import _write_official_owners_and_sales

        _write_official_owners_and_sales(self.location, {"owner_name": ["Old Owner LLC"]})
        _write_official_owners_and_sales(
            self.location, {"owner_name": ["New Owner LLC"], UNANSWERED_SOURCES_KEY: _TIER_UNANSWERED}
        )

        self.assertEqual(self._linked(), {"Old Owner LLC", "New Owner LLC"})

    def test_a_record_that_names_its_gaps_only_vaguely_is_partial_too(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import _write_official_owners_and_sales

        _write_official_owners_and_sales(self.location, {"owner_name": ["Old Owner LLC"]})
        _write_official_owners_and_sales(
            self.location, {"owner_name": ["New Owner LLC"], UNANSWERED_SOURCES_KEY: ["unknown"]}
        )

        self.assertEqual(self._linked(), {"Old Owner LLC", "New Owner LLC"})

    def test_a_record_missing_only_a_section_still_settles_who_owns_it(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import _write_official_owners_and_sales

        _write_official_owners_and_sales(self.location, {"owner_name": ["Old Owner LLC"]})
        _write_official_owners_and_sales(
            self.location, {"owner_name": ["New Owner LLC"], UNANSWERED_SOURCES_KEY: ["liens", "assessments:ny_orps"]}
        )

        self.assertEqual(self._linked(), {"New Owner LLC"})

    def test_a_complete_record_unlinks_the_owner_it_no_longer_names(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import _write_official_owners_and_sales

        _write_official_owners_and_sales(self.location, {"owner_name": ["Old Owner LLC"]})
        _write_official_owners_and_sales(self.location, {"owner_name": ["New Owner LLC"]})

        self.assertEqual(self._linked(), {"New Owner LLC"})


_ANY_SECTION = {"count": 0, "complete": True, "results": [], "providers": [], "next": None}


class SectionRefusalTests(_Case):
    """Only a section REData could not answer for now is named; one it refused for good is not (review finding 1).

    Asked through the real gateway, so each refusal is classified as REData's status code makes it.
    """

    def setUp(self) -> None:
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def _unanswered(self, section: str, status: int, body: object, headers: dict[str, str] | None = None) -> list[Any]:
        """The sections named unanswered when ``section`` answers ``status``, through the breaker a real session has."""
        from urbanlens.dashboard.plugins.builtin.property_records import _add_sections

        def request(_method: str, url: str, **_kwargs: object) -> mock.Mock:
            failing = f"/{section}/" in url
            code = status if failing else 200
            response = mock.Mock(status_code=code, headers=(headers or {}) if failing else {}, text="", ok=code == 200)
            response.json.return_value = body if failing else _ANY_SECTION
            return response

        inner = mock.Mock()
        inner.request.side_effect = request
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key")
        # The gateway's own rate-limited session, so REData's breaker sees every answer; only the wire is stubbed.
        cast("Any", gateway.session)._session = inner
        payload: dict[str, Any] = {"uuid": "parcel-1", "available": True}
        _add_sections(payload, gateway, "parcel-1")
        return list(payload.get(UNANSWERED_SOURCES_KEY) or [])

    def test_a_section_the_key_may_not_read_is_not_asked_for_hourly(self) -> None:
        for section in ("demographics", "owners", "liens"):
            with self.subTest(section=section):
                self.assertEqual(self._unanswered(section, 403, {"detail": "You do not have permission."}), [])

    def test_a_refusal_the_breaker_holds_for_the_next_parcel_is_not_asked_for_hourly_either(self) -> None:
        """After one 403 the breaker holds the endpoint for an hour, so the next parcel's call never goes out."""
        refused = {"detail": "You do not have permission."}

        self.assertEqual(self._unanswered("owners", 403, refused), [])
        self.assertEqual(self._unanswered("owners", 403, refused), [])

    def test_a_section_redata_has_no_such_parcel_for_is_not_asked_for_hourly(self) -> None:
        for body in ({"detail": "Not found."}, {"error": "no_data_found", "message": "none"}):
            with self.subTest(body=body):
                self.assertEqual(self._unanswered("national-parks", 404, body), [])

    def test_a_section_redata_could_not_answer_for_now_is_named(self) -> None:
        cases: tuple[tuple[int, dict[str, str], dict[str, str]], ...] = (
            (503, {"error": "source_error", "message": "upstream timed out"}, {}),
            (500, {"detail": "Server error"}, {}),
            (429, {"detail": "throttled"}, {"Retry-After": "30"}),
        )
        for status, body, headers in cases:
            with self.subTest(status=status):
                # A 429 holds REData's whole pool, so the sections asked after it go unanswered too.
                self.assertIn("liens", self._unanswered("liens", status, body, headers))


class PartialBoundaryTests(_Case):
    """REData 0.3.7 names the sources its unfiltered ``/boundaries/`` could not hear from (review finding 2)."""

    _CANDIDATE = {
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-73.931, 41.732], [-73.929, 41.732], [-73.929, 41.734], [-73.931, 41.734], [-73.931, 41.732]]
            ],
        },
        "is_suggested": True,
        "kind": "area",
    }

    def test_the_gateway_keeps_the_sources_boundaries_did_not_hear_from(self) -> None:
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import UNANSWERED_SOURCES_HEADER

        session = mock.Mock()
        response = mock.Mock(status_code=200, headers={UNANSWERED_SOURCES_HEADER: "ny_cris, tigerweb"}, text="")
        response.json.return_value = [self._CANDIDATE]
        session.get.return_value = response

        answer = RedataGateway(
            base_url="https://redata.example.test", api_key="test-key", session=session
        ).lookup_boundaries("parcel-1")

        self.assertEqual((answer.candidates, answer.unanswered_sources), ([self._CANDIDATE], ("ny_cris", "tigerweb")))

    def test_a_scored_boundary_from_a_partial_answer_is_deferred_not_drawn(self) -> None:
        from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
        from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import ParcelBoundaries

        gateway = mock.Mock()
        gateway.lookup_boundaries.return_value = ParcelBoundaries([self._CANDIDATE], unanswered_sources=("ny_cris",))

        with self.assertRaises(BoundaryProviderDeferredError):
            RedataBoundaryProvider()._scored_boundary(gateway, "parcel-1")

    def test_a_scored_boundary_from_a_complete_answer_is_drawn(self) -> None:
        from django.contrib.gis.geos import Polygon

        from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import ParcelBoundaries

        gateway = mock.Mock()
        gateway.lookup_boundaries.return_value = ParcelBoundaries([self._CANDIDATE])

        self.assertIsInstance(RedataBoundaryProvider()._scored_boundary(gateway, "parcel-1"), Polygon)

    def test_a_hull_of_a_partial_building_list_is_deferred_however_many_it_names(self) -> None:
        from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
        from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider

        corners = ((41.7325, -73.9305), (41.7325, -73.9295), (41.7335, -73.9295), (41.7335, -73.9305))
        buildings = [
            {**_BUILDING, "name": f"Hall {n}", "latitude": lat, "longitude": lng}
            for n, (lat, lng) in enumerate(corners)
        ]
        gateway = mock.Mock()
        gateway.lookup_parcel_buildings.return_value = ParcelBuildings(buildings, unanswered_sources=("overture",))

        with self.assertRaises(BoundaryProviderDeferredError):
            RedataBoundaryProvider()._buildings_convex_hull(gateway, "parcel-1", 41.733, -73.93)

        gateway.lookup_parcel_buildings.return_value = ParcelBuildings(buildings)
        self.assertIsNotNone(RedataBoundaryProvider()._buildings_convex_hull(gateway, "parcel-1", 41.733, -73.93))
