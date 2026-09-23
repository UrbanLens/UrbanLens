"""The ranked metric for a place's automatic name, and the alias rules that go with it (D-record in docs/INDEX.md).

Reproduces the HRSH courtyard pin on k3s-staging (41.73266, -73.92736): the wiki was titled "Courtyard Drive"
(Nominatim's reverse geocode, a private service road) while the National Register listing containing the point
was "Hudson River State Hospital, Main Building", and the parcel carried a CRIS building's name found by radius.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.name_resolution import NameCandidate, RuleBasedNameResolver
from urbanlens.dashboard.services.locations.name_tiers import NameTier, NamingScope, nominatim_tier, rank_key
from urbanlens.dashboard.services.locations.naming import (
    external_name_candidates_for_location,
    update_location_name_from_external_sources,
)

_LAT, _LON = 41.73266, -73.92736
_NRHP = "Hudson River State Hospital, Main Building"
_WIKIPEDIA = "Hudson River State Hospital"
_BLDG45 = "BLDG 45/MORTUARY & LAB (1896)"
_ROAD = "Courtyard Drive"

_COURTYARD_DRIVE = {
    "name": _ROAD,
    "type": "service",
    "category": "",
    "osm_url": "https://www.openstreetmap.org/way/352353227",
    "building": "",
    "amenity": "",
    "tourism": "",
    "historic": "",
}


class NominatimTierTests(SimpleTestCase):
    def test_a_service_road_is_road_tier(self) -> None:
        self.assertEqual(nominatim_tier(_COURTYARD_DRIVE), NameTier.ROAD)

    def test_a_house_address_is_road_tier(self) -> None:
        self.assertEqual(
            nominatim_tier({"name": "138", "type": "house", "osm_url": "https://osm.org/node/1"}), NameTier.ROAD
        )

    def test_a_named_amenity_area_is_a_site(self) -> None:
        data = {
            "name": "Vassar College",
            "type": "university",
            "category": "amenity",
            "osm_url": "https://www.openstreetmap.org/way/9",
        }
        self.assertEqual(nominatim_tier(data), NameTier.SITE)

    def test_a_named_node_is_a_poi(self) -> None:
        data = {
            "name": "Corner Cafe",
            "type": "cafe",
            "amenity": "cafe",
            "osm_url": "https://www.openstreetmap.org/node/9",
        }
        self.assertEqual(nominatim_tier(data), NameTier.POI)

    def test_a_named_building_is_building_tier(self) -> None:
        data = {
            "name": "Old Mill",
            "type": "yes",
            "category": "building",
            "building": "yes",
            "osm_url": "https://www.openstreetmap.org/way/9",
        }
        self.assertEqual(nominatim_tier(data), NameTier.BUILDING)


class RankingTests(SimpleTestCase):
    _CANDIDATES = [
        NameCandidate(_ROAD, "nominatim", NameTier.ROAD),
        NameCandidate(_BLDG45, "cris", NameTier.BUILDING),
        NameCandidate(_WIKIPEDIA, "wikipedia", NameTier.ENCYCLOPEDIA),
        NameCandidate(_NRHP, "historic_register", NameTier.HISTORIC_REGISTER),
    ]

    def _winner(self, candidates: list[NameCandidate], scope: NamingScope = NamingScope.PARCEL) -> str | None:
        resolved = RuleBasedNameResolver(["nominatim", "wikipedia", "nps", "google_places"], scope=scope).resolve(
            candidates, None
        )
        return resolved.name if resolved else None

    def test_the_register_listing_outranks_every_other_source(self) -> None:
        self.assertEqual(self._winner(self._CANDIDATES), _NRHP)

    def test_without_a_listing_the_article_wins(self) -> None:
        self.assertEqual(self._winner(self._CANDIDATES[:3]), _WIKIPEDIA)

    def test_a_road_is_the_last_resort(self) -> None:
        self.assertEqual(
            self._winner([self._CANDIDATES[0], NameCandidate("Corner Cafe", "google_places", NameTier.POI)]),
            "Corner Cafe",
        )
        self.assertEqual(self._winner([self._CANDIDATES[0]]), _ROAD)

    def test_agreement_does_not_lift_a_worse_tier_over_a_better_one(self) -> None:
        agreeing_roads = [
            NameCandidate(_ROAD, "nominatim", NameTier.ROAD),
            NameCandidate(_ROAD, "photon", NameTier.ROAD),
        ]
        self.assertEqual(
            self._winner([*agreeing_roads, NameCandidate(_WIKIPEDIA, "wikipedia", NameTier.ENCYCLOPEDIA)]), _WIKIPEDIA
        )

    def test_a_building_scope_puts_the_building_first(self) -> None:
        self.assertEqual(self._winner(self._CANDIDATES, NamingScope.BUILDING), _BLDG45)

    def test_rank_key_orders_tiers_for_the_scope(self) -> None:
        self.assertLess(
            rank_key(NameTier.HISTORIC_REGISTER, NamingScope.PARCEL),
            rank_key(NameTier.ENCYCLOPEDIA, NamingScope.PARCEL),
        )
        self.assertLess(
            rank_key(NameTier.BUILDING, NamingScope.BUILDING),
            rank_key(NameTier.HISTORIC_REGISTER, NamingScope.BUILDING),
        )


def _square(lon: float, lat: float, size: float) -> MultiPolygon:
    ring = ((lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _Fixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=_LAT, longitude=_LON, official_name="")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="HRSH", parent_pin=None)
        with writing_as(WriteSource.AUTOMATIC):
            self.wiki, _ = Wiki.objects.get_or_create_for_location(self.location)
        Pin.objects.filter(pk=self.pin.pk).update(wiki=self.wiki)
        LocationCache.set(self.location, "nominatim", _COURTYARD_DRIVE, query_key="q")
        LocationCache.set(
            self.location,
            "cris_building_usn",
            {"USNName": _BLDG45, "source_latitude": 41.733016, "source_longitude": -73.92638},
            query_key="q",
        )

    def _parcel(self, buildings: int) -> Place:
        parcel = Place.objects.create(kind=PlaceKind.PARCEL, geometry=_square(-73.934, 41.730, 0.011))
        Place.objects.filter(pk=parcel.pk).update(domain_root=parcel.pk, building_child_count=buildings)
        Location.objects.filter(pk=self.location.pk).update(place=parcel)
        self.location.refresh_from_db()
        return parcel

    def _register(self, *, contains: bool) -> None:
        row = {
            "name": _NRHP,
            "provider": "nps_nrhp",
            "scope": "structure",
            "resource_type": "building",
            "contains_point": contains,
        }
        LocationCache.set(self.location, "redata_historic_registers", {"resources": [row]}, query_key="q")

    def _names(self) -> set[str]:
        return {candidate.name for candidate in external_name_candidates_for_location(self.location)}


class BuildingAdmissionTests(_Fixture):
    def test_without_a_parcel_a_building_found_by_radius_lends_no_name(self) -> None:
        self.assertNotIn(_BLDG45, self._names())

    def test_a_parcel_of_many_buildings_lends_none_of_their_names(self) -> None:
        self._parcel(buildings=42)
        self.assertNotIn(_BLDG45, self._names())

    def test_the_only_building_on_the_parcel_may_name_it(self) -> None:
        self._parcel(buildings=1)
        self.assertIn(_BLDG45, self._names())

    def test_the_only_building_must_be_on_the_parcel(self) -> None:
        self._parcel(buildings=1)
        LocationCache.set(
            self.location,
            "cris_building_usn",
            {"USNName": _BLDG45, "source_latitude": 41.70, "source_longitude": -73.90},
            query_key="q",
        )
        self.assertNotIn(_BLDG45, self._names())

    def test_a_building_scoped_location_keeps_its_building_name(self) -> None:
        self._parcel(buildings=42)
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=baker.make(Pin, profile=self.profile))
        self.assertIn(_BLDG45, self._names())


class RegisterNameTests(_Fixture):
    def test_a_listing_containing_the_point_is_a_candidate(self) -> None:
        self._register(contains=True)
        self.assertIn(_NRHP, self._names())

    def test_a_listing_merely_nearby_is_not(self) -> None:
        self._register(contains=False)
        self.assertNotIn(_NRHP, self._names())

    def test_the_cris_national_register_listing_containing_the_point_is_a_candidate(self) -> None:
        LocationCache.set(
            self.location,
            "cris_building_usn",
            {"district": {"HistoricName": _NRHP, "resource_type": "national_register_listing", "contains_point": True}},
            query_key="q",
        )
        self.assertIn(_NRHP, self._names())


class TitleTests(_Fixture):
    def test_the_courtyard_pin_is_titled_by_its_register_listing(self) -> None:
        self._parcel(buildings=42)
        self._register(contains=True)
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _NRHP)

    def test_a_road_name_is_replaced_when_a_better_source_arrives(self) -> None:
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _ROAD)

        self._register(contains=True)
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _NRHP)

    def test_a_name_a_person_wrote_is_never_replaced(self) -> None:
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            self.wiki.name = "The Asylum"
            self.wiki.save(update_fields=["name", "updated"])
        self._register(contains=True)
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "The Asylum")

    def test_a_worse_tier_does_not_replace_a_better_automatic_name(self) -> None:
        self._register(contains=True)
        update_location_name_from_external_sources(self.location)
        LocationCache.objects.filter(location=self.location, source="redata_historic_registers").delete()
        LocationCache.set(
            self.location, "wikipedia", {"title": _WIKIPEDIA, "url": "https://en.wikipedia.org/wiki/x"}, query_key="q"
        )
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _NRHP)


class AliasRuleTests(_Fixture):
    def _wiki_aliases(self) -> set[str]:
        return set(WikiAlias.objects.filter(wiki=self.wiki).values_list("name", flat=True))

    def test_register_and_article_titles_become_aliases_and_the_road_does_not(self) -> None:
        self._parcel(buildings=42)
        self._register(contains=True)
        LocationCache.set(
            self.location, "wikipedia", {"title": _WIKIPEDIA, "url": "https://en.wikipedia.org/wiki/x"}, query_key="q"
        )
        update_location_name_from_external_sources(self.location)
        aliases = self._wiki_aliases()
        self.assertIn(_NRHP, aliases)
        self.assertIn(_WIKIPEDIA, aliases)
        self.assertNotIn(_ROAD, aliases)
        self.assertNotIn(_BLDG45, aliases)
        pin_aliases = set(PinAlias.objects.filter(pin=self.pin).values_list("name", flat=True))
        self.assertIn(_NRHP, pin_aliases)
        self.assertNotIn(_BLDG45, pin_aliases)

    def test_building_and_road_aliases_automation_added_are_pruned(self) -> None:
        WikiAlias.objects.create(wiki=self.wiki, name=_BLDG45, kind=AliasType.OFFICIAL, source="cris")
        WikiAlias.objects.create(wiki=self.wiki, name=_ROAD, kind=AliasType.OFFICIAL, source="nominatim")
        PinAlias.objects.create(pin=self.pin, name=_BLDG45, kind=AliasType.OFFICIAL, source="cris")
        PinAlias.objects.create(pin=self.pin, name=_ROAD, kind=AliasType.OFFICIAL, source="wiki_sync")
        self._parcel(buildings=42)
        self._register(contains=True)
        update_location_name_from_external_sources(self.location)
        self.assertNotIn(_BLDG45, self._wiki_aliases())
        self.assertNotIn(_ROAD, self._wiki_aliases())
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name__in=[_BLDG45, _ROAD]).exists())

    def test_a_persons_alias_is_never_pruned(self) -> None:
        PinAlias.objects.create(pin=self.pin, name=_BLDG45, kind=AliasType.ALTERNATE, source="user")
        self._parcel(buildings=42)
        update_location_name_from_external_sources(self.location)
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name=_BLDG45).exists())


_CAMPUS_LISTING_GEOMETRY = {
    "type": "Polygon",
    "coordinates": [[[-73.934, 41.730], [-73.923, 41.730], [-73.923, 41.737], [-73.934, 41.737], [-73.934, 41.730]]],
}


class ContainmentIsRecordedAtFetchTests(SimpleTestCase):
    def test_register_rows_record_whether_their_boundary_contains_the_point(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.redata_historic_registers import HistoricRegisterPanelSource
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

        rows = [
            {"name": _NRHP, "provider": "nps_nrhp", "geometry": _CAMPUS_LISTING_GEOMETRY},
            {"name": "Roosevelt, Isaac, House", "provider": "nps_nrhp", "geometry": None},
        ]
        envelope = LocationContextEnvelope(count=2, complete=True, results=rows)
        module = "urbanlens.dashboard.services.apis.locations.redata_cultural_resources_gateway"
        with (
            mock.patch(f"{module}.applicable_provider_tags", return_value=["nps_nrhp"]),
            mock.patch(f"{module}.RedataCulturalResourcesGateway") as gateway,
        ):
            gateway.return_value.near_resources.return_value = envelope
            source = HistoricRegisterPanelSource()
            stored = source.transform_rows(source.fetch_envelope(_LAT, _LON).results)
        self.assertEqual([row["contains_point"] for row in stored], [True, False])

    def test_a_cris_site_record_records_whether_it_contains_the_point(self) -> None:
        from urbanlens.dashboard.plugins.builtin.cris_buildings import site_resource_attributes

        listing = {
            "provider": "ny_cris",
            "resource_type": "national_register_listing",
            "attributes": {"HistoricName": _NRHP},
            "geometry": _CAMPUS_LISTING_GEOMETRY,
        }
        self.assertTrue(site_resource_attributes([listing], _LAT, _LON)["contains_point"])
        self.assertFalse(site_resource_attributes([listing], 41.70, -73.90)["contains_point"])


class RegisterArrivalRenamesTests(_Fixture):
    def test_a_register_row_landing_later_renames_an_automatic_road_name(self) -> None:
        update_location_name_from_external_sources(self.location)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _ROAD)

        with self.captureOnCommitCallbacks(execute=True):
            self._register(contains=True)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _NRHP)
