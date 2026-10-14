"""The chain of building record providers: who answers a parcel's building list, and what a failure of one costs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import PARTIAL_ANSWER_STALE_AFTER, LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.parcel_buildings import (
    OverpassBuildingRecords,
    ParcelBuildingsEnrichmentSource,
    ParcelBuildingsPanelSource,
)
from urbanlens.dashboard.plugins.points import BUILDING_RECORD_PROVIDERS
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.places import building_records
from urbanlens.dashboard.services.places.building_records import (
    BuildingRecordProvider,
    BuildingsAnswer,
    building_key,
    fetch_parcel_buildings,
)

from .place_helpers import official_geometry


class _DownError(GatewayRequestError):
    """A source that could not be asked."""


class _ComputingError(UpstreamBusyError):
    answer_pending: ClassVar[bool] = True


class _Fake(BuildingRecordProvider):
    """A provider answering whatever it was given."""

    def __init__(
        self,
        key: str,
        buildings: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        unanswered: tuple[str, ...] = (),
        primary: bool = True,
        accepts_supplements: bool = False,
        supplement: list[dict[str, Any]] | None = None,
        available: bool = True,
    ) -> None:
        self.key = key
        self.tag = key
        self.primary = primary
        self.accepts_supplements = accepts_supplements
        self._buildings = buildings or []
        self._error = error
        self._unanswered = unanswered
        self._supplement = supplement or []
        self._available = available
        self.asked = 0

    def available(self) -> bool:
        return self._available

    def buildings_on_parcel(self, location: Location) -> BuildingsAnswer:
        self.asked += 1
        if self._error is not None:
            raise self._error
        return BuildingsAnswer(self._buildings, self._unanswered)

    def supplementary_records(self, location: Location) -> list[dict[str, Any]]:
        self.asked += 1
        if self._error is not None:
            raise self._error
        return self._supplement

    def merge_supplement(
        self, buildings: list[dict[str, Any]], supplement: list[dict[str, Any]], polygon: object
    ) -> list[dict[str, Any]]:
        return [*buildings, *({**record, "merged": True} for record in supplement)]


_HALL = {"name": "Main Hall", "ref": "a:1", "latitude": 41.7331, "longitude": -73.9301}
_SHED = {"name": "Shed", "ref": "b:1", "latitude": 41.7332, "longitude": -73.9302}
_LODGE = {"name": "Lodge", "ref": "c:1", "latitude": 41.7333, "longitude": -73.9303}


def _location() -> Location:
    return baker.make(Location, latitude=41.7331, longitude=-73.9301, google_place=None)


class BuildingKeyTests(SimpleTestCase):
    def test_a_stable_ref_is_the_key(self) -> None:
        self.assertEqual(building_key({"ref": "x:1", "stable_ref": "x:stable"}), "x:stable")

    def test_the_ref_is_the_key_without_one(self) -> None:
        self.assertEqual(building_key({"ref": " x:1 "}), "x:1")

    def test_a_record_with_neither_has_no_key(self) -> None:
        self.assertEqual(building_key({}), "")


class ChainTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = _location()

    def test_the_first_provider_that_finds_buildings_answers(self) -> None:
        first, second = _Fake("first", [_HALL]), _Fake("second", [_SHED])
        with BUILDING_RECORD_PROVIDERS.overridden([first, second]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload, {"buildings": [_HALL], "provider": "first"})
        self.assertEqual(second.asked, 0)

    def test_an_empty_answer_passes_to_the_next_provider(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first"), _Fake("second", [_SHED])]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "second")

    def test_a_provider_that_cannot_be_asked_is_skipped(self) -> None:
        absent = _Fake("absent", [_HALL], available=False)
        with BUILDING_RECORD_PROVIDERS.overridden([absent, _Fake("second", [_SHED])]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "second")
        self.assertEqual(absent.asked, 0)

    def test_nothing_found_by_providers_that_all_answered_is_empty(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first"), _Fake("second")]):
            self.assertEqual(fetch_parcel_buildings(self.location), {})

    def test_no_providers_is_empty(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([]):
            self.assertEqual(fetch_parcel_buildings(self.location), {})

    def test_a_partial_answer_names_what_did_not_answer(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL], unanswered=("county",))]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["unanswered_sources"], ["county"])

    def test_an_answer_found_unaided_is_not_marked(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL])]):
            self.assertNotIn("unanswered_sources", fetch_parcel_buildings(self.location))

    def test_an_outage_with_nothing_else_found_raises(self) -> None:
        """``{}`` here would be cached as "no buildings" for the whole cache term."""
        with (
            BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", error=_DownError("down")), _Fake("second")]),
            self.assertRaises(_DownError),
        ):
            fetch_parcel_buildings(self.location)

    def test_an_outage_still_returns_what_the_next_provider_found(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", error=_DownError("down")), _Fake("second", [_SHED])]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "second")
        self.assertEqual(
            payload["unanswered_sources"], ["first"], "a list standing in for one that was out of reach is a floor"
        )

    def test_an_answer_the_provider_is_still_computing_stops_the_chain(self) -> None:
        """No fallback stands in for a list that will arrive."""
        second = _Fake("second", [_SHED])
        with (
            BUILDING_RECORD_PROVIDERS.overridden(
                [_Fake("first", error=_ComputingError("busy", retry_after=30)), second]
            ),
            self.assertRaises(_ComputingError),
        ):
            fetch_parcel_buildings(self.location)

        self.assertEqual(second.asked, 0)

    def test_an_error_that_is_not_an_outage_propagates(self) -> None:
        with (
            BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", error=ValueError("bug"))]),
            self.assertRaises(ValueError),
        ):
            fetch_parcel_buildings(self.location)


class SupplementTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = _location()

    def test_a_supplement_is_folded_into_a_provider_that_takes_one(self) -> None:
        primary = _Fake("primary", [_HALL], accepts_supplements=True)
        extra = _Fake("extra", primary=False, supplement=[_SHED])
        with BUILDING_RECORD_PROVIDERS.overridden([primary, extra]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "primary")
        self.assertEqual([record["name"] for record in payload["buildings"]], ["Main Hall", "Shed"])
        self.assertTrue(payload["buildings"][1]["merged"])

    def test_a_provider_that_takes_none_is_not_supplemented(self) -> None:
        extra = _Fake("extra", primary=False, supplement=[_SHED])
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("primary", [_HALL]), extra]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["buildings"], [_HALL])
        self.assertEqual(extra.asked, 0)

    def test_a_supplement_stands_in_when_no_primary_found_a_building(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden(
            [_Fake("primary"), _Fake("extra", primary=False, supplement=[_SHED])]
        ):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "extra")
        self.assertEqual([record["name"] for record in payload["buildings"]], ["Shed"])

    def test_a_supplement_that_could_not_be_asked_marks_the_list(self) -> None:
        primary = _Fake("primary", [_HALL], accepts_supplements=True)
        with BUILDING_RECORD_PROVIDERS.overridden([primary, _Fake("extra", primary=False, error=_DownError("down"))]):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["buildings"], [_HALL])
        self.assertEqual(payload["unanswered_sources"], ["extra"])


class RegistryTests(SimpleTestCase):
    def test_places_are_filed_under_the_first_namespace_a_provider_declares(self) -> None:
        class Namespaced(_Fake):
            place_namespace = "ns"

        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("none"), Namespaced("named", [_HALL])]):
            self.assertEqual(building_records.preferred_place_namespace(), "ns")
            self.assertIsNotNone(building_records.place_provider("ns"))
            self.assertIsNone(building_records.place_provider("other"))
            self.assertIsNone(building_records.place_provider(""))

    def test_no_namespace_is_blank(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("none")]):
            self.assertEqual(building_records.preferred_place_namespace(), "")

    def test_labels_and_service_keys_come_from_the_providers_that_can_be_asked(self) -> None:
        class Labelled(_Fake):
            source_labels = {"survey": "County Survey"}
            service_keys = ("survey_api",)

        with BUILDING_RECORD_PROVIDERS.overridden([Labelled("one", available=False), Labelled("two")]):
            self.assertEqual(building_records.source_labels(), {"survey": "County Survey"})
            self.assertEqual(building_records.service_keys(), ("survey_api",))

    def test_bulk_enrichment_needs_a_provider_that_allows_it_and_can_be_asked(self) -> None:
        class Bulk(_Fake):
            bulk_enrichment = True

        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("one"), Bulk("two", available=False)]):
            self.assertFalse(building_records.bulk_enrichment_available())
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("one"), Bulk("two")]):
            self.assertTrue(building_records.bulk_enrichment_available())


class OverpassProviderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = _location()

    def _osm(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "Powerhouse",
                "building_number": "",
                "latitude": 41.7331,
                "longitude": -73.9301,
                "osm_id": 5,
                "source": "osm",
            }
        ]

    def _bounded(self) -> None:
        square = MultiPolygon(
            Polygon.from_bbox((-73.932, 41.731, -73.928, 41.735)),
            srid=4326,
        )
        official_geometry(self.location, square)

    def test_it_lists_the_osm_buildings_inside_the_property_boundary(self) -> None:
        self._bounded()
        with (
            BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords()]),
            patch.object(OverpassGateway, "__post_init__", lambda _self: None),
            patch.object(OverpassGateway, "buildings_within", return_value=self._osm()),
        ):
            payload = fetch_parcel_buildings(self.location)

        self.assertEqual(payload["provider"], "osm")
        self.assertEqual(payload["buildings"], self._osm())

    def test_no_real_property_boundary_skips_overpass_entirely(self) -> None:
        """The synthesized circle isn't a parcel - counting inside it would catch neighbours."""
        with (
            BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords()]),
            patch.object(OverpassGateway, "buildings_within") as overpass,
        ):
            payload = fetch_parcel_buildings(self.location)

        overpass.assert_not_called()
        self.assertEqual(payload, {})

    def test_an_outage_with_nothing_else_found_raises(self) -> None:
        self._bounded()
        with (
            BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords()]),
            patch.object(OverpassGateway, "__post_init__", lambda _self: None),
            patch.object(OverpassGateway, "buildings_within", side_effect=OSError("overpass down")),
            self.assertRaises(OSError),
        ):
            fetch_parcel_buildings(self.location)

    def test_a_failure_that_is_not_an_outage_is_nothing_found(self) -> None:
        self._bounded()
        with (
            BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords()]),
            patch.object(OverpassGateway, "__post_init__", lambda _self: None),
            patch.object(OverpassGateway, "buildings_within", side_effect=ValueError("malformed ring")),
        ):
            self.assertEqual(fetch_parcel_buildings(self.location), {})

    def test_it_is_the_public_source_and_files_places_under_osm(self) -> None:
        provider = OverpassBuildingRecords()
        self.assertTrue(provider.available())
        self.assertEqual(provider.place_namespace, "osm")
        self.assertFalse(provider.bulk_enrichment)


class PartialAnswerCachingTests(TestCase):
    """A list a source says is missing something is shown, and asked for again within the hour."""

    def setUp(self) -> None:
        super().setUp()
        self.location = _location()
        self.pin = baker.make(Pin, profile=baker.make("dashboard.Profile"), location=self.location)

    def _fresh_after(self, elapsed: timedelta) -> bool:
        with patch("django.utils.timezone.now", return_value=timezone.now() + elapsed):
            return LocationCache.get_fresh(self.location, PARCEL_BUILDINGS_CACHE_SOURCE) is not None

    def test_the_panel_keeps_a_partial_list_only_briefly(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL], unanswered=("county",))]):
            ParcelBuildingsPanelSource().fetch(self.pin)

        self.assertTrue(self._fresh_after(timedelta(0)))
        self.assertFalse(self._fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))

    def test_background_enrichment_keeps_a_partial_list_only_briefly(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL], unanswered=("county",))]):
            ParcelBuildingsEnrichmentSource().enrich(self.location)

        self.assertTrue(self._fresh_after(timedelta(0)))
        self.assertFalse(self._fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))

    def test_a_complete_list_keeps_the_full_window(self) -> None:
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL])]):
            ParcelBuildingsPanelSource().fetch(self.pin)

        self.assertTrue(self._fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))


class EnrichmentSourceTests(TestCase):
    def test_fetch_returns_the_payload_and_a_coordinate_query_key(self) -> None:
        location = baker.make(Location, latitude="41.733150", longitude="-73.930370", google_place=None)
        with BUILDING_RECORD_PROVIDERS.overridden([_Fake("first", [_HALL, _SHED])]):
            payload, query_key = ParcelBuildingsEnrichmentSource().fetch(location)

        self.assertEqual(len(payload["buildings"]), 2)
        self.assertEqual(query_key, "41.73315,-73.93037")

    def test_the_source_runs_only_where_a_provider_allows_a_bulk_fill(self) -> None:
        class Bulk(_Fake):
            bulk_enrichment = True

        source = ParcelBuildingsEnrichmentSource()
        with BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords()]):
            self.assertFalse(source.gate())
        with BUILDING_RECORD_PROVIDERS.overridden([OverpassBuildingRecords(), Bulk("bulk")]):
            self.assertTrue(source.gate())

    def test_it_spends_the_service_keys_of_the_providers_that_can_be_asked(self) -> None:
        class Keyed(_Fake):
            service_keys = ("survey_api",)

        with BUILDING_RECORD_PROVIDERS.overridden(
            [OverpassBuildingRecords(), Keyed("keyed"), Keyed("absent", available=False)]
        ):
            self.assertEqual(ParcelBuildingsEnrichmentSource().service_keys, ("overpass", "survey_api"))
