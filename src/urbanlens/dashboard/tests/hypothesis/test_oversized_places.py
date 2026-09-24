"""P148: a county-sized "parcel" put strangers across a county into one wiki and pin-in-common domain.

Dev place 1740 ("103 Schermerhorn Rd, Cohoes") covered 1,322 km² and held 105 locations across the Capital
District. It was the convex hull of the CRIS buildings REData linked to one house lot, taken as the parcel when
REData's boundary ranking timed out.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant, PlaceKind, PlaceStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_SOURCE_ERROR,
    PropertyRecordsUnavailableError,
)
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
from urbanlens.dashboard.services.pins.common_pins import common_pin_location_ids
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.places.provisioning import provision_places_for_coordinate, upsert_place
from urbanlens.dashboard.services.places.splits import looks_like_a_split
from urbanlens.dashboard.services.wiki.wiki_access import accessible_domain_ids, location_visible_to
from urbanlens.UrbanLens.settings.app import settings

from .test_places_campus import make_place, square

# Two of place 1740's real members, 35 km apart.
_WEST = (42.827143, -74.110174)
_EAST = (42.746971, -73.693808)
# About 1,400 km², holding both.
_COUNTY = square(-73.9, 42.8, 0.25)


def _located(lat: float, lng: float) -> Location:
    return Location.objects.create(latitude=round(lat, 6), longitude=round(lng, 6))


def _pin(profile: Profile, location: Location) -> Pin:
    return baker.make(Pin, profile=profile, location=location)


class _Answers:
    """A provider that answers every coordinate with fixed polygons."""

    def __init__(self, key: str, property_polygon=None, building_polygon=None) -> None:
        self.service_key = key
        self.boundary_kind = "property"
        self._answer = {"property": property_polygon, "building": building_polygon}

    def get_typed_boundaries(self, latitude, longitude, *, name=None):
        return self._answer


class ChainCapTests(SimpleTestCase):
    """No provider's answer is taken as a parcel when it is the size of a county."""

    def test_a_county_sized_property_polygon_is_not_a_parcel(self) -> None:
        parcel = square(-74.110174, 42.827143, 0.001)
        chain = BoundaryProviderChain(providers=(_Answers("redata_boundary", _COUNTY), _Answers("overpass", parcel)))

        resolved = chain.get_boundaries(*_WEST)

        self.assertTrue(resolved.property_polygon.equals(parcel), "the county-sized outline won the parcel slot")
        self.assertEqual([key for key, _polygon in resolved.property_candidates], ["overpass"])

    def test_a_building_footprint_the_size_of_a_town_is_not_a_building(self) -> None:
        chain = BoundaryProviderChain(providers=(_Answers("overpass", None, square(-73.9, 42.8, 0.02)),))

        self.assertIsNone(chain.get_boundaries(42.8, -73.9).building_polygon)


class UpsertCapTests(TestCase):
    def test_a_county_sized_parcel_is_never_created(self) -> None:
        self.assertIsNone(upsert_place(PlaceKind.PARCEL, _COUNTY))
        self.assertFalse(Place.objects.exists())

    def test_an_ordinary_parcel_still_is(self) -> None:
        self.assertIsNotNone(upsert_place(PlaceKind.PARCEL, square(-73.9, 42.8, 0.001)))


class ResolutionGuardTests(TestCase):
    """Rows already in the database from before the cap must stop resolving."""

    def setUp(self) -> None:
        super().setUp()
        self.county = make_place(PlaceKind.PARCEL, _COUNTY)

    def test_a_coordinate_never_resolves_onto_it(self) -> None:
        self.assertIsNone(Place.objects.resolve_for_point(*_WEST))

    def test_a_building_in_its_domain_is_not_resolvable_either(self) -> None:
        make_place(PlaceKind.BUILDING, square(_WEST[1], _WEST[0], 0.0002), parent=self.county)

        self.assertIsNone(Place.objects.resolve_for_point(*_WEST))

    def test_a_real_parcel_under_it_still_resolves(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, square(_WEST[1], _WEST[0], 0.001))

        self.assertEqual(Place.objects.resolve_for_point(*_WEST), parcel)


class DomainGuardTests(TestCase):
    """Strangers pinned 35 km apart were one wiki domain and had a pin in common."""

    def setUp(self) -> None:
        super().setUp()
        self.county = make_place(PlaceKind.PARCEL, _COUNTY)
        self.first = baker.make(User).profile
        self.second = baker.make(User).profile
        self.west = _located(*_WEST)
        self.east = _located(*_EAST)
        for location in (self.west, self.east):
            resolution.attach_location(location, self.county)
        _pin(self.first, self.west)
        _pin(self.second, self.east)

    def test_strangers_across_a_county_have_no_pin_in_common(self) -> None:
        self.assertFalse(Profile._have_common_pin(self.first, self.second))
        self.assertFalse(Profile._have_common_pin(self.second, self.first))
        self.assertEqual(common_pin_location_ids([self.first, self.second]), set())

    def test_the_same_location_still_counts(self) -> None:
        _pin(self.second, self.west)

        self.assertTrue(Profile._have_common_pin(self.first, self.second))
        self.assertEqual(common_pin_location_ids([self.first, self.second]), {self.west.pk})

    def test_it_is_never_a_wiki_domain(self) -> None:
        baker.make(Wiki, location=self.east, name="East")

        self.assertNotIn(self.county.pk, accessible_domain_ids(self.first))
        self.assertFalse(location_visible_to(self.east, self.first))

    def test_a_building_under_it_does_not_reach_it(self) -> None:
        building = make_place(PlaceKind.BUILDING, square(_WEST[1], _WEST[0], 0.0002), parent=self.county)
        resolution.attach_location(self.west, building)

        self.assertNotIn(self.county.pk, accessible_domain_ids(self.first))
        self.assertFalse(Profile._have_common_pin(self.first, self.second))

    def test_a_grant_on_it_grants_nothing(self) -> None:
        PlaceAccessGrant.objects.create(
            profile=self.first, place=self.county, reason=GrantReason.GRANDFATHERED_ENGAGEMENT
        )

        self.assertNotIn(self.county.pk, accessible_domain_ids(self.first))


class SplitGuardTests(TestCase):
    """Correcting a county-sized parcel is not a subdivision.

    A split grandfathers every holder of the old parcel into every successor, so treating the correction as one
    would give strangers across the county permanent access to each other's real parcels.
    """

    def test_a_county_sized_parcel_shrinking_is_not_a_split(self) -> None:
        county = make_place(PlaceKind.PARCEL, _COUNTY)

        self.assertFalse(looks_like_a_split(county, square(_WEST[1], _WEST[0], 0.001)))

    def test_a_real_parcel_shrinking_still_is(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, square(_WEST[1], _WEST[0], 0.005))

        self.assertTrue(looks_like_a_split(parcel, square(_WEST[1], _WEST[0], 0.001)))


class ProvisioningReproductionTests(TestCase):
    """The 02:19 run end to end, with REData's answers mocked and every other provider left out."""

    _GATEWAY = "urbanlens.dashboard.services.apis.locations.boundaries.redata.RedataGateway"

    def setUp(self) -> None:
        super().setUp()
        for attribute, value in (("redata_api_url", "https://redata.example.test"), ("redata_api_key", "test-key")):
            patcher = mock.patch.object(settings, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        only_redata = mock.patch(
            "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain",
            return_value=BoundaryProviderChain(providers=(RedataBoundaryProvider(),)),
        )
        only_redata.start()
        self.addCleanup(only_redata.stop)
        self.west = _located(*_WEST)
        self.east = _located(*_EAST)

    @staticmethod
    def _spread_buildings() -> list[dict]:
        """On-property records whose hull spans both locations, as REData flagged them."""
        corners = [(42.68, -74.68), (43.0, -74.68), (42.78, -73.67), (42.68, -73.69), (42.83, -74.11)]
        return [
            {"latitude": lat, "longitude": lng, "match_scope": "survey_roster", "is_on_property": True}
            for lat, lng in corners
        ]

    def _provision_west(self, gateway) -> None:
        gateway.return_value.lookup_parcel.return_value = {"uuid": "781dd879"}
        gateway.return_value.lookup_buildings.return_value = self._spread_buildings()
        provision_places_for_coordinate(self.west)
        self.west.refresh_from_db()
        self.east.refresh_from_db()

    def test_a_ranking_timeout_does_not_make_the_buildings_hull_the_parcel(self) -> None:
        with mock.patch(self._GATEWAY) as gateway:
            gateway.return_value.lookup_boundaries.side_effect = PropertyRecordsUnavailableError(
                REASON_SOURCE_ERROR, "Could not reach REData: Read timed out. (read timeout=30)"
            )
            self._provision_west(gateway)

        self.assertIsNone(self.east.place_id, "a location 35 km away was swept onto the new parcel")
        self.assertFalse(Place.objects.filter(area_sqm__gt=10_000_000).exists())

    def test_without_a_ranking_the_hull_still_stays_on_the_property(self) -> None:
        with mock.patch(self._GATEWAY) as gateway:
            gateway.return_value.lookup_boundaries.return_value = []
            self._provision_west(gateway)

        self.assertIsNone(self.east.place_id, "a location 35 km away was swept onto the new parcel")
        self.assertFalse(Place.objects.filter(area_sqm__gt=10_000_000).exists())


class DetachOversizedPlacesCommandTests(TestCase):
    """The repair for rows created before the fix."""

    def setUp(self) -> None:
        super().setUp()
        self.first = baker.make(User).profile
        self.second = baker.make(User).profile
        self.county = make_place(PlaceKind.PARCEL, _COUNTY, name="103 Schermerhorn Rd")
        self.real_parcel = make_place(PlaceKind.PARCEL, square(_EAST[1], _EAST[0], 0.001))
        self.building = make_place(PlaceKind.BUILDING, square(-73.8, 42.9, 0.0002), parent=self.county)

        self.west = _located(*_WEST)
        self.east = _located(*_EAST)
        self.on_building = _located(42.9, -73.8)
        for location in (self.west, self.east):
            resolution.attach_location(location, self.county)
        resolution.attach_location(self.on_building, self.building)
        # Attached directly, as the rows written before the fix were.
        _pin(self.first, self.west)
        _pin(self.second, self.east)
        _pin(self.second, self.on_building)

        self.parent_wiki = baker.make(Wiki, location=self.west, name="West")
        self.child_wiki = baker.make(Wiki, location=self.east, name="East", parent_wiki=self.parent_wiki)

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("detach_oversized_places", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_it_takes_the_county_out_of_every_domain(self) -> None:
        self._run()

        self.county.refresh_from_db()
        self.building.refresh_from_db()
        for location in (self.west, self.east, self.on_building):
            location.refresh_from_db()
        self.child_wiki.refresh_from_db()

        self.assertEqual(self.county.status, PlaceStatus.SUPERSEDED)
        self.assertIsNone(self.building.parent_id)
        self.assertEqual(self.building.domain_root_id, self.building.pk)
        self.assertIsNone(self.west.place_id)
        self.assertIsNone(self.west.place_resolved_at, "a detached location must be asked about again")
        self.assertEqual(self.east.place_id, self.real_parcel.pk)
        self.assertEqual(self.on_building.place_id, self.building.pk)
        self.assertIsNone(self.child_wiki.parent_wiki_id)
        self.assertFalse(Profile._have_common_pin(self.first, self.second))

    def test_it_is_idempotent(self) -> None:
        self._run()
        snapshot = (
            list(Place.objects.order_by("pk").values_list("pk", "status", "parent_id", "domain_root_id")),
            list(Location.objects.order_by("pk").values_list("pk", "place_id", "place_resolved_at")),
            list(Wiki.objects.order_by("pk").values_list("pk", "parent_wiki_id")),
        )

        second = self._run()

        self.assertEqual(
            snapshot,
            (
                list(Place.objects.order_by("pk").values_list("pk", "status", "parent_id", "domain_root_id")),
                list(Location.objects.order_by("pk").values_list("pk", "place_id", "place_resolved_at")),
                list(Wiki.objects.order_by("pk").values_list("pk", "parent_wiki_id")),
            ),
        )
        self.assertIn("0 place(s)", second)

    def test_a_dry_run_writes_nothing(self) -> None:
        self._run("--dry-run")

        self.county.refresh_from_db()
        self.west.refresh_from_db()
        self.assertEqual(self.county.status, PlaceStatus.CURRENT)
        self.assertEqual(self.west.place_id, self.county.pk)

    def test_an_ordinary_parcel_is_left_alone(self) -> None:
        self._run()

        self.real_parcel.refresh_from_db()
        self.assertEqual(self.real_parcel.status, PlaceStatus.CURRENT)


class FixtureSanityTests(SimpleTestCase):
    def test_the_county_fixture_is_county_sized(self) -> None:
        """Guards the fixture: the tests above mean nothing if it were parcel-sized."""
        self.assertGreater(_COUNTY.transform(6933, clone=True).area, 1_000_000_000)
