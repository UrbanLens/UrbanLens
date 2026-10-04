"""A location standing on one of its campus's buildings opens that building's wiki, never the campus's (P261).

A building with no place of its own resolves onto its parcel, whose wiki another location holds, so
``Wiki.objects.existing_for_location`` used to hand every such location the campus's wiki.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.queryset import WikiManager
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.places.scope import effective_pin_type
from urbanlens.dashboard.services.wiki.wiki_naming import wiki_named_by_location
from urbanlens.dashboard.tasks import ensure_wiki_for_location

from .place_helpers import make_place

_LAT, _LNG = 41.7333, -73.9281

_A = (41.7343, -73.9271)  # a footprint, its wiki at its centre
_B = (41.7323, -73.9291)  # a point only, its wiki on it
_C = (41.7323, -73.9288)  # a point only, about 25 m east of B, no wiki
_E = (41.7333, -73.9295)  # an envelope, its wiki at its centre
_F = (41.7335, -73.9297)  # a wing inside the envelope, no wiki


def _square(latitude: float, longitude: float, half: float) -> MultiPolygon:
    ring = (
        (longitude - half, latitude - half),
        (longitude + half, latitude - half),
        (longitude + half, latitude + half),
        (longitude - half, latitude + half),
        (longitude - half, latitude - half),
    )
    return MultiPolygon(Polygon(ring), srid=4326)


def _geojson(latitude: float, longitude: float, half: float) -> dict:
    ring = [
        [longitude - half, latitude - half],
        [longitude + half, latitude - half],
        [longitude + half, latitude + half],
        [longitude - half, latitude + half],
        [longitude - half, latitude - half],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _record(ref: str, point: tuple[float, float], *, footprint: float | None = None, parent: str = "") -> dict:
    record = {"ref": ref, "name": f"Building {ref}", "latitude": point[0], "longitude": point[1], "source": "redata"}
    if footprint is not None:
        record["geometry"] = _geojson(*point, footprint)
    if parent:
        record["parent_ref"] = parent
    return record


class _Campus(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, _square(_LAT, _LNG, 0.002), name="Campus parcel")
        # Point-only building places: they make the parcel a campus, and no point resolves onto them.
        for _ in range(2):
            make_place(PlaceKind.BUILDING, None, parent=self.parcel)
        self.parcel.refresh_from_db()
        self.campus_location = self.location_at(_LAT, _LNG)
        self.campus_wiki, _ = Wiki.objects.get_or_create_for_location(self.campus_location)
        LocationCache.set(
            self.campus_location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {
                "buildings": [
                    _record("A", _A, footprint=0.0002),
                    _record("B", _B),
                    _record("C", _C),
                    _record("E", _E, footprint=0.0004),
                    _record("F", _F, footprint=0.00005, parent="E"),
                ]
            },
        )
        self.wiki_a = self.building_wiki(*_A, "Building A")
        self.wiki_b = self.building_wiki(*_B, "Building B")
        self.wiki_e = self.building_wiki(*_E, "Building E")

    def location_at(self, latitude: float, longitude: float) -> Location:
        location = baker.make(Location, latitude=f"{latitude:.6f}", longitude=f"{longitude:.6f}", google_place=None)
        location.refresh_from_db()
        return location

    def building_wiki(self, latitude: float, longitude: float, name: str) -> Wiki:
        """A building's wiki as the mirror makes them: on the parcel's place, holding none, nested under the campus's."""
        return Wiki.objects.create(
            location=self.location_at(latitude, longitude),
            place=None,
            name=name,
            parent_wiki=self.campus_wiki,
            pin_type=PinType.BUILDING,
        )


class FixtureTests(_Campus):
    def test_every_location_stands_on_the_campus_parcel(self) -> None:
        self.assertEqual(self.campus_wiki.place_id, self.parcel.pk)
        self.assertGreaterEqual(self.parcel.building_child_count, 2)
        for wiki in (self.wiki_a, self.wiki_b, self.wiki_e):
            self.assertEqual(wiki.location.place_id, self.parcel.pk)


class ExistingForLocationTests(_Campus):
    def test_a_point_on_a_buildings_footprint_opens_its_wiki(self) -> None:
        """About 25 m from the wiki's own point: the footprint decides."""
        location = self.location_at(_A[0] + 0.00018, _A[1] + 0.00018)

        self.assertEqual(Wiki.objects.existing_for_location(location), self.wiki_a)

    def test_a_point_near_a_point_only_building_opens_its_wiki(self) -> None:
        location = self.location_at(_B[0] + 0.00005, _B[1])

        self.assertEqual(Wiki.objects.existing_for_location(location), self.wiki_b)

    def test_a_nearer_building_without_a_wiki_wins_over_one_with(self) -> None:
        """Within 15 m of both B and C, nearer C: C's, which has no wiki yet."""
        location = self.location_at(_B[0], _B[1] + 0.00017)

        self.assertIsNone(Wiki.objects.existing_for_location(location))

    def test_the_grounds_open_the_campus_wiki(self) -> None:
        location = self.location_at(_LAT - 0.0015, _LNG + 0.0015)

        self.assertEqual(Wiki.objects.existing_for_location(location), self.campus_wiki)

    def test_a_child_pins_community_wiki_is_its_buildings(self) -> None:
        """The reported shape: a building child pin standing apart from its building's wiki."""
        campus_pin = baker.make(Pin, profile=self.profile, location=self.campus_location, parent_pin=None)
        child = baker.make(
            Pin,
            profile=self.profile,
            location=self.location_at(_B[0] + 0.00005, _B[1]),
            parent_pin=campus_pin,
            pin_type=PinType.BUILDING,
        )

        self.assertEqual(child.community_wiki, self.wiki_b)

    def test_resolution_reads_no_pin(self) -> None:
        location = self.location_at(_B[0] + 0.00005, _B[1])
        baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)

        with CaptureQueriesContext(connection) as queries:
            Wiki.objects.existing_for_location(location)

        self.assertFalse([query["sql"] for query in queries if "dashboard_user_pins" in query["sql"]])

    def test_the_batched_lookup_agrees(self) -> None:
        locations = [
            self.location_at(_A[0] + 0.00018, _A[1] + 0.00018),
            self.location_at(_B[0] + 0.00005, _B[1]),
            self.location_at(_B[0], _B[1] + 0.00017),
            self.location_at(_LAT - 0.0015, _LNG + 0.0015),
        ]
        pins = [baker.make(Pin, profile=self.profile, location=location, wiki=None) for location in locations]

        batched = Boundary.objects._wikis_for_pins(list(zip(pins, locations, strict=True)))

        for pin, location in zip(pins, locations, strict=True):
            wiki = Wiki.objects.existing_for_location(location)
            self.assertEqual(batched.get(pin.pk), wiki.pk if wiki is not None else None)


class OrdinaryParcelTests(TestCase):
    def test_a_second_point_on_a_one_building_property_opens_its_wiki(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, _square(_LAT, _LNG, 0.0005))
        first = baker.make(Location, latitude=f"{_LAT:.6f}", longitude=f"{_LNG:.6f}", google_place=None)
        wiki, _ = Wiki.objects.get_or_create_for_location(first)
        LocationCache.set(first, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [_record("H", (_LAT + 0.0002, _LNG))]})
        second = baker.make(Location, latitude=f"{_LAT + 0.0002:.6f}", longitude=f"{_LNG:.6f}", google_place=None)

        self.assertEqual(second.place_id, parcel.pk)
        self.assertEqual(Wiki.objects.existing_for_location(second), wiki)


class CreationTests(_Campus):
    def test_a_building_without_a_wiki_gets_one_nested_under_the_campus(self) -> None:
        location = self.location_at(_B[0], _B[1] + 0.00017)

        wiki = Wiki.objects.get(pk=ensure_wiki_for_location(location.pk))

        self.assertEqual(wiki.location_id, location.pk)
        self.assertIsNone(wiki.place_id)
        self.assertEqual(wiki.parent_wiki_id, self.campus_wiki.pk)
        self.assertEqual(Wiki.objects.existing_for_location(location), wiki)
        self.assertEqual((wiki.pin_type, wiki.pin_type_is_user_provided), (PinType.BUILDING, False))
        self.assertEqual(effective_pin_type(wiki), PinType.BUILDING)

    def test_a_wing_without_a_wiki_nests_under_its_envelopes(self) -> None:
        location = self.location_at(_F[0] + 0.00002, _F[1])

        wiki, created = Wiki.objects.get_or_create_for_location(location)

        self.assertTrue(created)
        self.assertEqual(wiki.parent_wiki_id, self.wiki_e.pk)

    def test_a_buildings_new_wiki_takes_in_no_other_root_wiki_on_the_campus(self) -> None:
        """The parcel's outline is the campus wiki's, not a building's to nest other wikis by."""
        stray = Wiki.objects.create(location=self.location_at(_LAT - 0.0015, _LNG + 0.0015), place=None, name="Stray")
        location = self.location_at(_B[0], _B[1] + 0.00017)

        Wiki.objects.get_or_create_for_location(location)

        stray.refresh_from_db()
        self.assertNotEqual(stray.parent_wiki_id, Wiki.objects.get(location=location).pk)

    def test_a_campus_wiki_appearing_meanwhile_still_leaves_the_building_its_own(self) -> None:
        """The first lookup misses the campus's wiki, as a create racing it would; the insert then collides on the place."""
        location = self.location_at(_B[0], _B[1] + 0.00017)
        real = WikiManager._resolve
        calls: list[int] = []

        def first_misses(manager, *args, **kwargs):
            calls.append(1)
            return (None, None) if len(calls) == 1 else real(manager, *args, **kwargs)

        with patch.object(WikiManager, "_resolve", first_misses):
            wiki, created = Wiki.objects.get_or_create_for_location(location)

        self.assertTrue(created)
        self.assertEqual((wiki.location_id, wiki.place_id, wiki.pin_type), (location.pk, None, PinType.BUILDING))
        self.assertEqual(Wiki.objects.get(pk=wiki.pk).parent_wiki_id, self.campus_wiki.pk)

    def test_an_existing_buildings_wiki_is_reused_not_duplicated(self) -> None:
        location = self.location_at(_B[0] + 0.00005, _B[1])

        wiki, created = Wiki.objects.get_or_create_for_location(location)

        self.assertFalse(created)
        self.assertEqual(wiki, self.wiki_b)


class NamingTests(_Campus):
    def test_a_root_pins_location_on_a_building_names_the_campus_wiki_not_the_buildings(self) -> None:
        """A root pin's names describe the property; a building's wiki is named from its own location only."""
        location = self.location_at(_B[0] + 0.00005, _B[1])
        baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

        self.assertEqual(wiki_named_by_location(location), self.campus_wiki)

    def test_a_child_pins_location_on_a_building_names_no_wiki(self) -> None:
        location = self.location_at(_B[0] + 0.00005, _B[1])
        campus_pin = baker.make(Pin, profile=self.profile, location=self.campus_location, parent_pin=None)
        baker.make(Pin, profile=self.profile, location=location, parent_pin=campus_pin)

        self.assertIsNone(wiki_named_by_location(location))


class PlacedBuildingTests(_Campus):
    def test_a_location_on_a_building_place_keeps_that_places_wiki(self) -> None:
        """Where the building has a place of its own, nothing changes."""
        building = make_place(PlaceKind.BUILDING, _square(*_C, 0.0001), parent=self.parcel)
        location = self.location_at(*_C)
        resolution.attach_location(location, building)
        holder = Wiki.objects.create(location=self.location_at(_C[0] + 0.00005, _C[1]), place=building, name="C")

        self.assertEqual(Wiki.objects.existing_for_location(location), holder)
