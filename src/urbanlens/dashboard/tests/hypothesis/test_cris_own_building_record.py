"""A building's CRIS card holds a record only when that record's own point is on the building (P255).

On the dev stack, HRSH's "Building at Hudson River State Hospital" (location 98254) held "BLDG 166/OLD POLICE STATION
(1932)", the record another child pin's location (99719) also held, with no position: both fetch paths took the
nearest building from a 200 m lookup with no distance limit, and the first one when none had a position.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingEnrichmentSource, CrisBuildingPanelSource
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
from urbanlens.dashboard.services.locations import name_tiers, national_register, site_scope
from urbanlens.dashboard.services.locations.name_tiers import NamingScope

_LAT, _LNG = 41.73460, -73.92640
_OWN = (41.73468, -73.92650)
_NEIGHBOUR = (41.73462, -73.92643)


def _building(uuid: str, name: str, point: tuple[float, float] | None) -> dict:
    row = {
        "uuid": uuid,
        "provider": "ny_cris",
        "resource_type": "building",
        "attributes": {"USNName": name, "USNNum": uuid},
    }
    if point is not None:
        row.update(source_latitude=point[0], source_longitude=point[1])
    return row


_OWN_RECORD = _building("b-33", "BLDG 33/POWERHOUSE & MACHINE SHOP (1929)", _OWN)
_NEIGHBOURS_RECORD = _building("b-166", "BLDG 166/OLD POLICE STATION (1932)", _NEIGHBOUR)
_UNPOSITIONED_RECORD = _building("b-166", "BLDG 166/OLD POLICE STATION (1932)", None)


def _stands_on_own(_location, latitude, longitude, **_kwargs) -> bool:
    return (latitude, longitude) == _OWN


class _Building(SimpleTestCase):
    scope = NamingScope.BUILDING

    def setUp(self) -> None:
        super().setUp()
        self.location = Location(latitude=_LAT, longitude=_LNG)
        for patcher in (
            patch.object(name_tiers, "naming_scope", side_effect=lambda _location: self.scope),
            patch.object(national_register, "stands_on", side_effect=_stands_on_own),
            patch.object(site_scope, "site_buildings", return_value=[]),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _enrich(self, resources: list[dict]) -> dict | None:
        with patch.object(RedataGateway, "lookup_cultural_resources", return_value=resources):
            payload, _query_key = CrisBuildingEnrichmentSource().fetch(self.location)
        return payload


class EnrichmentTakesOnlyTheBuildingsOwnRecordTests(_Building):
    def test_a_nearer_neighbours_record_is_passed_over_for_the_buildings_own(self) -> None:
        payload = self._enrich([_NEIGHBOURS_RECORD, _OWN_RECORD])

        assert payload is not None
        self.assertEqual(payload["resource_uuid"], "b-33")

    def test_a_neighbours_record_alone_is_not_this_buildings(self) -> None:
        self.assertIsNone(self._enrich([_NEIGHBOURS_RECORD]))

    def test_a_record_without_a_position_is_no_buildings(self) -> None:
        self.assertIsNone(self._enrich([_UNPOSITIONED_RECORD]))


class APropertyStillTakesTheNearestRecordTests(_Building):
    scope = NamingScope.PARCEL

    def test_a_property_takes_the_nearest_building_as_before(self) -> None:
        payload = self._enrich([_OWN_RECORD, _NEIGHBOURS_RECORD])

        assert payload is not None
        self.assertEqual(payload["resource_uuid"], "b-166")


class PanelTakesOnlyTheBuildingsOwnRecordTests(_Building):
    def _fetch(self, resources: list[dict]) -> dict:
        pin = SimpleNamespace(pk=1, location=self.location, location_id=None)
        with (
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=resources),
            patch.object(
                RedataGateway,
                "fetch_cultural_resource_detail",
                side_effect=lambda uuid: next(row for row in resources if row["uuid"] == uuid),
            ),
            patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=False),
            patch.object(LocationCache, "set") as cache_set,
            patch.object(CrisBuildingPanelSource, "_request_extractions"),
            patch.object(CrisBuildingPanelSource, "_link_register_listings"),
        ):
            CrisBuildingPanelSource()._fetch_now(pin)
        return cache_set.call_args.args[2]

    def test_the_card_holds_the_buildings_own_record_not_the_nearer_neighbours(self) -> None:
        self.assertEqual(self._fetch([_NEIGHBOURS_RECORD, _OWN_RECORD])["resource_uuid"], "b-33")

    def test_with_only_a_neighbours_record_the_card_holds_nothing(self) -> None:
        self.assertEqual(self._fetch([_NEIGHBOURS_RECORD, _UNPOSITIONED_RECORD]), {})


def _roster_entry(uuid: str, point: tuple[float, float]) -> dict:
    return {
        "resource_uuid": uuid,
        "name": uuid,
        "attributes": {"USNName": uuid},
        "source_latitude": point[0],
        "source_longitude": point[1],
        "detailed": True,
    }


class SiteAnswerTakesOnlyTheBuildingsOwnEntryTests(_Building):
    def test_the_nearest_roster_entry_is_skipped_when_it_is_not_on_the_building(self) -> None:
        site_data = {
            "campus_buildings": [_roster_entry("b-166", _NEIGHBOUR), _roster_entry("b-33", _OWN)],
            "attachments": [],
        }

        answer = CrisBuildingPanelSource()._answer_from_site(site_data, self.location)

        assert answer is not None
        self.assertEqual(answer["resource_uuid"], "b-33")

    def test_no_entry_on_the_building_answers_nothing(self) -> None:
        site_data = {"campus_buildings": [_roster_entry("b-166", _NEIGHBOUR)], "attachments": []}

        self.assertIsNone(CrisBuildingPanelSource()._answer_from_site(site_data, self.location))
