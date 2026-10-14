"""
The ranked metric for a place's automatic name, and what a name provider declares to take part in it.

Core knows the tiers and the rules. A source says which tier it gives, whether its candidate names a child pin
outright, and whether the building it names stands on a location or parcel; core asks it, and names no source of its own.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.plugins.points import NAME_PROVIDERS
from urbanlens.dashboard.services.locations.name_resolution import NameCandidate, NameProvider, RuleBasedNameResolver
from urbanlens.dashboard.services.locations.name_tiers import (
    DEFAULT_TIER,
    NameTier,
    NamingScope,
    building_name_admissible,
    nominatim_tier,
    rank_key,
    source_tiers,
    tier_for,
)
from urbanlens.dashboard.services.locations.naming import external_name_candidates_for_location

_LAT, _LON = 41.73266, -73.92736
_WIKIPEDIA = "Hudson River State Hospital"
_LISTING = "Hudson River State Hospital, Main Building"
_BUILDING = "BLDG 45/MORTUARY & LAB (1896)"
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


class _Named(NameProvider):
    """A provider answering one fixed name, standing where it is told to."""

    def __init__(
        self, source: str, name: str, *, on_location: bool = True, on_parcel: bool = True, **declared: Any
    ) -> None:
        super().__init__(source=source, **declared)
        self._name = name
        self._on_location = on_location
        self._on_parcel = on_parcel

    def candidates(self, location: Location) -> list[str | None]:
        return [self._name]

    def stands_on_location(self, location: Location) -> bool:
        return self._on_location

    def stands_on_parcel(self, location: Location, parcel: Place) -> bool:
        return self._on_parcel


class NominatimTierTests(SimpleTestCase):
    def test_a_service_road_is_road_tier(self) -> None:
        self.assertEqual(nominatim_tier(_COURTYARD_DRIVE), NameTier.ROAD)

    def test_a_house_address_is_road_tier(self) -> None:
        self.assertEqual(nominatim_tier({**_COURTYARD_DRIVE, "type": "house"}), NameTier.ROAD)

    def test_a_named_amenity_area_is_a_site(self) -> None:
        site = {
            "name": "Mill Park",
            "type": "park",
            "category": "leisure",
            "osm_url": "https://www.openstreetmap.org/way/1",
            "leisure": "park",
        }
        self.assertEqual(nominatim_tier(site), NameTier.SITE)

    def test_a_named_node_is_a_poi(self) -> None:
        shop = {
            "name": "Corner Cafe",
            "type": "cafe",
            "category": "amenity",
            "osm_url": "https://www.openstreetmap.org/node/1",
        }
        self.assertEqual(nominatim_tier(shop), NameTier.POI)

    def test_a_named_building_is_building_tier(self) -> None:
        self.assertEqual(nominatim_tier({"name": "Hall", "category": "building", "type": "yes"}), NameTier.BUILDING)


class RankingTests(SimpleTestCase):
    _CANDIDATES = [
        NameCandidate(_ROAD, "nominatim", NameTier.ROAD),
        NameCandidate(_BUILDING, "building_record", NameTier.BUILDING),
        NameCandidate(_WIKIPEDIA, "wikipedia", NameTier.ENCYCLOPEDIA),
        NameCandidate(_LISTING, "historic_register", NameTier.HISTORIC_REGISTER),
    ]

    def _winner(self, candidates: list[NameCandidate], scope: NamingScope = NamingScope.PARCEL) -> str | None:
        resolved = RuleBasedNameResolver(["nominatim", "wikipedia", "google_places"], scope=scope).resolve(
            candidates, None
        )
        return resolved.name if resolved else None

    def test_the_article_outranks_the_register_listing(self) -> None:
        """A listing can name one building on a larger plot; the article names the property."""
        self.assertEqual(self._winner(self._CANDIDATES), _WIKIPEDIA)

    def test_without_an_article_the_register_listing_wins(self) -> None:
        self.assertEqual(self._winner([c for c in self._CANDIDATES if c.tier != NameTier.ENCYCLOPEDIA]), _LISTING)

    def test_a_road_is_the_last_resort(self) -> None:
        corner = NameCandidate("Corner Cafe", "google_places", NameTier.POI)
        self.assertEqual(self._winner([self._CANDIDATES[0], corner]), "Corner Cafe")
        self.assertEqual(self._winner([self._CANDIDATES[0]]), _ROAD)

    def test_a_building_scope_puts_the_building_first(self) -> None:
        self.assertEqual(self._winner(self._CANDIDATES, NamingScope.BUILDING), _BUILDING)

    def test_rank_key_orders_tiers_for_the_scope(self) -> None:
        self.assertLess(
            rank_key(NameTier.ENCYCLOPEDIA, NamingScope.PARCEL),
            rank_key(NameTier.HISTORIC_REGISTER, NamingScope.PARCEL),
        )
        self.assertLess(
            rank_key(NameTier.BUILDING, NamingScope.BUILDING),
            rank_key(NameTier.HISTORIC_REGISTER, NamingScope.BUILDING),
        )


class SourceTierTests(SimpleTestCase):
    def test_a_provider_declares_the_tier_of_its_names(self) -> None:
        with NAME_PROVIDERS.overridden(
            [_Named("survey", "Hall", tier=NameTier.BUILDING), _Named("park_service", "Park", tier=NameTier.SITE)]
        ):
            self.assertEqual(source_tiers()["survey"], NameTier.BUILDING)
            self.assertEqual(tier_for("park_service", None), NameTier.SITE)

    def test_a_provider_that_declares_none_ranks_as_the_default(self) -> None:
        with NAME_PROVIDERS.overridden([_Named("undeclared", "Thing")]):
            self.assertNotIn("undeclared", source_tiers())
            self.assertEqual(tier_for("undeclared", None), DEFAULT_TIER)

    def test_the_public_sources_keep_their_tiers_whoever_contributes(self) -> None:
        with NAME_PROVIDERS.overridden([]):
            self.assertEqual(tier_for("wikipedia", None), NameTier.ENCYCLOPEDIA)
            self.assertEqual(tier_for("google_places", None), NameTier.POI)

    def test_a_source_nothing_contributes_any_more_is_unknown(self) -> None:
        """A stored alias from a withdrawn plugin ranks as the default, and is never judged outranked."""
        with NAME_PROVIDERS.overridden([]):
            self.assertEqual(tier_for("withdrawn_source", None), DEFAULT_TIER)


def _square(lon: float, lat: float, size: float) -> MultiPolygon:
    ring = ((lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class BuildingAdmissionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=_LAT, longitude=_LON, official_name="")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="HRSH", parent_pin=None)

    def _parcel(self, buildings: int) -> None:
        parcel = Place.objects.create(kind=PlaceKind.PARCEL, geometry=_square(-73.934, 41.730, 0.011))
        Place.objects.filter(pk=parcel.pk).update(domain_root=parcel.pk, building_child_count=buildings)
        Location.objects.filter(pk=self.location.pk).update(place=parcel)
        self.location.refresh_from_db()

    def _as_child_pin(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=baker.make(Pin, profile=self.profile))

    def _names(self, **provider: Any) -> set[str]:
        with NAME_PROVIDERS.overridden([_Named("survey", _BUILDING, tier=NameTier.BUILDING, **provider)]):
            return {candidate.name for candidate in external_name_candidates_for_location(self.location)}

    def test_without_a_parcel_a_building_found_by_radius_lends_no_name(self) -> None:
        self.assertNotIn(_BUILDING, self._names())

    def test_a_parcel_of_many_buildings_lends_none_of_their_names(self) -> None:
        self._parcel(buildings=42)
        self.assertNotIn(_BUILDING, self._names())

    def test_the_only_building_on_the_parcel_may_name_it(self) -> None:
        self._parcel(buildings=1)
        self.assertIn(_BUILDING, self._names())

    def test_the_only_building_must_be_on_the_parcel(self) -> None:
        self._parcel(buildings=1)
        self.assertNotIn(_BUILDING, self._names(on_parcel=False))

    def test_a_building_scoped_location_keeps_its_building_name(self) -> None:
        self._parcel(buildings=42)
        self._as_child_pin()
        self.assertIn(_BUILDING, self._names())

    def test_a_building_scoped_location_takes_no_name_from_a_record_off_the_building(self) -> None:
        self._parcel(buildings=42)
        self._as_child_pin()
        self.assertNotIn(_BUILDING, self._names(on_location=False))

    def test_a_source_no_provider_contributes_is_not_held_to_stand_anywhere(self) -> None:
        self._parcel(buildings=1)
        with NAME_PROVIDERS.overridden([]):
            self.assertTrue(building_name_admissible("withdrawn_source", self.location))
