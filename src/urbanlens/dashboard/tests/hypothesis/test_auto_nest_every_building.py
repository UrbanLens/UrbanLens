"""Every building on a campus gets one child pin and one child wiki, linked to each other, without being asked."""

from __future__ import annotations

from itertools import combinations
import json
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.naming import is_meaningful_name
from urbanlens.dashboard.services.locations.site_scope import (
    BUILDING_MATCH_METERS,
    PARCEL_BUILDINGS_CACHE_SOURCE,
    meters_between,
)
from urbanlens.dashboard.services.pins.auto_nest import auto_nest_location, auto_nest_pin
from urbanlens.dashboard.services.places.lineage import refresh_derived_flags
from urbanlens.dashboard.tests.hypothesis.building_fixtures import (
    CAMPUS_LAT,
    CAMPUS_LNG,
    offset,
    parcel_square,
    record,
    rect,
    ring,
)

CAMPUS_NAME = "Hudson River State Hospital"
PRIVATE_NAME = "My secret way in"


def _campus_records() -> list[dict]:
    return [
        record("osm:way/1", 60, -80, geometry=rect(60, -80, 70, 20), name="BLDG 1/MAIN", building_number="1"),
        record(
            "osm:way/2",
            -60,
            80,
            geometry=rect(-60, 80, 30, 20),
            overlap_refs=["cris:2"],
            sources=[{"source": "overpass", "attributes": {"building": "yes"}}],
        ),
        record("cris:2", -60, 92, geometry=rect(-60, 92, 30, 20), overlap_refs=["osm:way/2"], name="Laundry"),
        record("osm:way/3", 100, 100, geometry=rect(100, 100, 60, 60), child_refs=["cris:3a", "cris:3b"]),
        record("cris:3a", 100, 85, parent_ref="osm:way/3", name="Chapel"),
        record("cris:3b", 100, 115, parent_ref="osm:way/3", name="Garage 3"),
        record(
            "cris:4",
            -120,
            -120,
            year_built=1925,
            sources=[
                {"source": "cris", "attributes": {}},
                {"source": "overpass", "attributes": {"building": "garage"}},
            ],
        ),
        record("cris:5", -120, -110),
        record("cris:6", 0, 50, is_on_property=False, name="Across the road"),
        record("osm:way/7", 400, 400, geometry=rect(400, 400, 20, 20), name="Neighbour"),
    ]


#: Records on the property and inside its boundary - every one of them is a panel row.
_ON_PROPERTY_REFS = {"osm:way/1", "osm:way/2", "cris:2", "osm:way/3", "cris:3a", "cris:3b", "cris:4", "cris:5"}


def _geos(geometry: dict) -> GEOSGeometry:
    return GEOSGeometry(json.dumps(geometry), srid=4326)


class CampusTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL, geometry=MultiPolygon(_geos(parcel_square())))
        self.location = baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LNG, place=self.parcel)
        self.campus_wiki = baker.make(
            Wiki, location=self.location, place=self.parcel, name=CAMPUS_NAME, parent_wiki=None
        )
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None, name=PRIVATE_NAME)

    def cache(self, buildings: list[dict], location: Location | None = None) -> None:
        LocationCache.set(
            location or self.location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": buildings, "provider": "redata"},
            query_key="test",
        )

    def descendants(self) -> list[Pin]:
        return list(self.pin.descendants().select_related("location"))


class EveryBuildingGetsOnePinTests(CampusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cache(_campus_records())

    def test_every_building_on_the_property_gets_exactly_one_pin(self) -> None:
        created = auto_nest_pin(self.pin)

        direct = list(self.pin.detail_pins.all())
        self.assertEqual(created, 6)
        self.assertEqual(
            len(direct), 4, "main block, the overlapping pair as one, the envelope, and the two close points as one"
        )
        self.assertEqual(
            {child.name for child in self.descendants()}, {"BLDG 1/MAIN", "Laundry", None, "Chapel", "Garage 3"}
        )
        self.assertFalse(any(child.name in {"Across the road", "Neighbour"} for child in self.descendants()))

    def test_contained_buildings_nest_under_their_container(self) -> None:
        auto_nest_pin(self.pin)

        chapel = Pin.objects.get(name="Chapel")
        garage = Pin.objects.get(name="Garage 3")
        self.assertEqual(chapel.parent_pin_id, garage.parent_pin_id)
        self.assertEqual(chapel.parent_pin.parent_pin_id, self.pin.pk)

    def test_no_two_direct_children_stand_within_the_match_radius(self) -> None:
        auto_nest_pin(self.pin)

        for first, second in combinations(self.pin.detail_pins.select_related("location"), 2):
            distance = meters_between(
                first.effective_latitude,
                first.effective_longitude,
                second.effective_latitude,
                second.effective_longitude,
            )
            self.assertGreaterEqual(
                distance, BUILDING_MATCH_METERS, f"{first} and {second} are two pins for one building"
            )

    def test_children_sit_inside_the_parcel_typed_as_buildings(self) -> None:
        auto_nest_pin(self.pin)

        parcel = self.parcel.geometry
        for child in self.descendants():
            self.assertEqual(child.pin_type, PinType.BUILDING)
            self.assertTrue(parcel.contains(Point(child.effective_longitude, child.effective_latitude, srid=4326)))

    def test_every_panel_row_names_its_pin(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource

        auto_nest_pin(self.pin)
        payload = ParcelBuildingsPanelSource().api_payload(self.pin)

        rows = payload["buildings"]
        self.assertEqual(len(rows), len(_ON_PROPERTY_REFS))
        self.assertEqual([row["name"] or row["building_number"] for row in rows if not row["child_pin_uuid"]], [])
        self.assertEqual(payload["unpinned_count"], 0)
        descendant_uuids = {str(child.uuid) for child in self.descendants()}
        self.assertTrue({row["child_pin_uuid"] for row in rows} <= descendant_uuids)

    def test_the_parents_map_lists_every_direct_child(self) -> None:
        auto_nest_pin(self.pin)
        self.client.force_login(self.user)

        response = self.client.get(reverse("pin.detail_pins.json", args=[self.pin.slug]))

        listed = {entry["uuid"] for entry in response.json()["detail_pins"]}
        self.assertEqual(listed, {str(child.uuid) for child in self.pin.detail_pins.all()})

    def test_the_location_sweep_reaches_every_users_campus_pin(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=self.location, parent_pin=None)

        auto_nest_location(self.location)

        self.assertEqual(self.pin.descendants().count(), 6)
        self.assertEqual(other.descendants().count(), 6)
        self.assertEqual(
            Wiki.objects.filter(parent_wiki=self.campus_wiki).count(), 4, "the second user's sweep reuses the wikis"
        )


class ChildWikiLinkTests(CampusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cache(_campus_records())
        auto_nest_pin(self.pin)

    def test_each_building_pin_stands_on_its_own_child_wiki(self) -> None:
        for child in self.descendants():
            wiki = Wiki.objects.get_for_location(child.location)
            self.assertIsNotNone(wiki, f"{child} has no wiki at its own location")
            self.assertEqual(wiki.location_id, child.location_id)
            self.assertNotEqual(wiki.pk, self.campus_wiki.pk)
            expected_parent = (
                self.campus_wiki
                if child.parent_pin_id == self.pin.pk
                else Wiki.objects.get_for_location(child.parent_pin.location)
            )
            self.assertEqual(wiki.parent_wiki_id, expected_parent.pk)

    def test_the_campus_wiki_holds_one_child_wiki_per_direct_building(self) -> None:
        self.assertEqual(self.campus_wiki.child_wikis.count(), 4)

    def test_child_wikis_are_meaningfully_and_publicly_named(self) -> None:
        names = {Wiki.objects.get_for_location(child.location).name for child in self.descendants()}

        for name in names:
            self.assertTrue(is_meaningful_name(name), name)
            self.assertFalse(name.lower().startswith("unnamed location"), name)
            self.assertNotEqual(name.lower(), CAMPUS_NAME.lower())
            self.assertNotIn(PRIVATE_NAME.lower(), name.lower())
        self.assertIn(
            "Garage (1925) at Hudson River State Hospital", names, "a nameless record falls back to what it is"
        )
        self.assertIn("Building at Hudson River State Hospital", names)

    def test_the_pin_page_links_its_own_wiki_first(self) -> None:
        from urbanlens.dashboard.services.places.ambiguity import linked_wiki_locations

        for child in self.descendants():
            links = linked_wiki_locations(child, self.profile)
            self.assertEqual(links[0].pk, child.location_id, f"{child} links elsewhere first")

    def test_the_rendered_pin_page_links_its_own_wiki_and_offers_a_floorplan(self) -> None:
        child = Pin.objects.get(name="Laundry")
        self.client.force_login(self.user)

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            response = self.client.get(reverse("pin.details", args=[child.slug]))

        content = response.content.decode()
        hrefs = re.findall(r'<a href="([^"]+)" class="pin-hero-wiki-box-link"', content)
        self.assertTrue(hrefs, "the building pin page links no wiki at all")
        self.assertEqual(hrefs[0], reverse("location.wiki", args=[child.location.slug]))
        self.assertNotEqual(hrefs[0], reverse("location.wiki", args=[self.location.slug]))
        self.assertIn('id="pin-detail-map-wrapper"', content)
        self.assertIn(reverse("pin.floorplan", args=[child.slug]), content)


class BuildingBoundaryTests(CampusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cache(_campus_records())
        auto_nest_pin(self.pin)

    def test_a_building_pins_detail_boundary_is_its_own_footprint(self) -> None:
        from urbanlens.dashboard.services.pins.pin_detail import build_pin_detail

        child = Pin.objects.get(name="BLDG 1/MAIN")
        boundary = build_pin_detail(child, self.profile)["boundary"]

        self.assertIsNotNone(boundary)
        footprint = _geos(rect(60, -80, 70, 20))
        self.assertAlmostEqual(_geos(boundary).area / footprint.area, 1.0, places=2)

    def test_the_campus_detail_boundary_is_still_the_parcel(self) -> None:
        from urbanlens.dashboard.services.pins.pin_detail import build_pin_detail

        boundary = build_pin_detail(self.pin, self.profile)["boundary"]

        self.assertAlmostEqual(_geos(boundary).area / self.parcel.geometry.area, 1.0, places=2)

    def test_the_floorplan_is_seeded_from_the_building_not_the_grounds(self) -> None:
        from urbanlens.dashboard.controllers.floorplans import _building_outline

        outline = _building_outline(Pin.objects.get(name="Laundry"))

        self.assertGreaterEqual(len(outline), 3)
        latitudes = [lat for lat, _lng in outline]
        self.assertLess(meters_between(min(latitudes), CAMPUS_LNG, max(latitudes), CAMPUS_LNG), 40)


class ResweepTests(CampusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.records = _campus_records()
        self.cache(self.records)
        auto_nest_pin(self.pin)
        self.before = {child.pk for child in self.descendants()}

    def test_a_second_sweep_changes_nothing(self) -> None:
        self.assertEqual(auto_nest_pin(self.pin), 0)
        self.assertEqual({child.pk for child in self.descendants()}, self.before)
        self.assertEqual(Wiki.objects.exclude(pk=self.campus_wiki.pk).count(), 6)

    def _rename_refs(self) -> None:
        """P7: REData's ref follows whichever source joined the cluster, so a CRIS timeout renames buildings."""
        renamed = {"osm:way/1": "cris:1", "osm:way/2": "cris:2", "cris:2": "osm:way/2", "cris:3a": "h:9#1"}
        for building in self.records:
            building["ref"] = renamed.get(building["ref"], building["ref"])
            building["parent_ref"] = renamed.get(building.get("parent_ref") or "", building.get("parent_ref"))
            building["overlap_refs"] = [renamed.get(ref, ref) for ref in building.get("overlap_refs") or []]
        self.cache(self.records)

    def test_a_ref_that_changes_between_responses_neither_duplicates_nor_merges(self) -> None:
        self._rename_refs()

        self.assertEqual(auto_nest_pin(self.pin), 0)
        self.assertEqual({child.pk for child in self.descendants()}, self.before)

    @pytest.mark.xfail(strict=True, reason="P7: ensure_building_places still keys building places by REData ref")
    def test_a_ref_that_changes_between_responses_reuses_the_building_place(self) -> None:
        before = Place.objects.filter(kind=PlaceKind.BUILDING).count()
        self._rename_refs()

        auto_nest_pin(self.pin)

        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), before)

    def test_a_building_that_appears_on_refresh_is_nested(self) -> None:
        self.records.append(record("osm:way/8", -150, 150, geometry=rect(-150, 150, 20, 15), name="Boiler house"))
        self.cache(self.records)

        self.assertEqual(auto_nest_pin(self.pin), 1)
        boiler = Pin.objects.get(name="Boiler house")
        self.assertEqual(boiler.parent_pin_id, self.pin.pk)
        self.assertEqual(Wiki.objects.get_for_location(boiler.location).parent_wiki_id, self.campus_wiki.pk)

    def test_a_deleted_auto_created_pin_stays_deleted(self) -> None:
        Pin.objects.get(name="BLDG 1/MAIN").delete()

        self.assertEqual(auto_nest_pin(self.pin), 0)
        self.assertFalse(Pin.objects.filter(name="BLDG 1/MAIN").exists())

    def test_a_moved_pin_is_not_replaced(self) -> None:
        main = Pin.objects.get(name="BLDG 1/MAIN")
        main.location = baker.make(Location, latitude=offset(-190, 0)[0], longitude=offset(-190, 0)[1])
        main.save(update_fields=["location"])

        self.assertEqual(auto_nest_pin(self.pin), 0)


class SweepGateTests(CampusTestCase):
    def test_no_real_boundary_means_no_sweep(self) -> None:
        """Nesting is a claim about the property; without its outline there is nothing to be inside."""
        self.location.place = None
        self.location.save(update_fields=["place"])
        self.campus_wiki.place = None
        self.campus_wiki.save(update_fields=["place"])
        self.cache(_campus_records())

        self.assertEqual(auto_nest_pin(self.pin), 0)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.buildings_auto_nested_at, "a sweep that could not run must not count as one")

    def test_a_campus_pin_resting_on_one_of_its_buildings_still_sweeps(self) -> None:
        """A parcel coordinate on a footprint re-homes the campus onto that building; the parcel is still above it."""
        building = baker.make(
            Place, kind=PlaceKind.BUILDING, geometry=MultiPolygon(_geos(rect(0, 0, 20, 20))), parent=self.parcel
        )
        baker.make(
            Place, kind=PlaceKind.BUILDING, geometry=MultiPolygon(_geos(rect(-150, -150, 10, 10))), parent=self.parcel
        )
        refresh_derived_flags([self.parcel.pk])
        self.location.place = building
        self.location.save(update_fields=["place"])
        self.cache(_campus_records())
        self.pin = Pin.objects.select_related("location").get(pk=self.pin.pk)
        self.assertIsNone(Boundary.objects.effective_polygon_for_pin(self.pin, BoundaryType.PROPERTY))

        self.assertEqual(auto_nest_pin(self.pin), 6)


class ChildWikiRaceTests(CampusTestCase):
    def test_a_root_wiki_already_standing_on_a_building_is_adopted_and_renamed(self) -> None:
        """A child pin's own save queues ensure_wiki_for_location; when it wins the race its wiki is the building's."""
        records = _campus_records()
        self.cache(records)
        from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings

        main_cluster = next(cluster for cluster in cluster_buildings(records) if "osm:way/1" in cluster.refs)
        occupied = Location.objects.get_exact_or_create(main_cluster.latitude, main_cluster.longitude)[0]
        squatter = baker.make(
            Wiki, location=occupied, place=None, parent_wiki=None, name="Unnamed Location in Poughkeepsie"
        )

        auto_nest_pin(self.pin)

        squatter.refresh_from_db()
        self.assertEqual(squatter.parent_wiki_id, self.campus_wiki.pk)
        self.assertEqual(squatter.name, "BLDG 1/MAIN")
        self.assertEqual(Pin.objects.get(name="BLDG 1/MAIN").location_id, occupied.pk)

    def test_a_wiki_renamed_to_the_campus_name_is_renamed_back_to_the_building(self) -> None:
        records = _campus_records()
        self.cache(records)
        from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings

        laundry = next(cluster for cluster in cluster_buildings(records) if "cris:2" in cluster.refs)
        occupied = Location.objects.get_exact_or_create(laundry.latitude, laundry.longitude)[0]
        squatter = baker.make(Wiki, location=occupied, place=None, parent_wiki=None, name=CAMPUS_NAME)

        auto_nest_pin(self.pin)

        squatter.refresh_from_db()
        self.assertEqual(squatter.name, "Laundry")

    def test_a_building_whose_place_the_campus_wiki_holds_still_gets_its_own_wiki(self) -> None:
        self.cache(_campus_records())
        main_place = baker.make(
            Place,
            kind=PlaceKind.BUILDING,
            geometry=MultiPolygon(_geos(rect(60, -80, 70, 20))),
            parent=self.parcel,
            provider="redata",
            provider_key="osm:way/1",
        )
        self.campus_wiki.place = main_place
        self.campus_wiki.save(update_fields=["place"])

        auto_nest_pin(self.pin)

        main = Pin.objects.get(name="BLDG 1/MAIN")
        wiki = Wiki.objects.get_for_location(main.location)
        self.assertNotEqual(wiki.pk, self.campus_wiki.pk)
        self.assertEqual(wiki.parent_wiki_id, self.campus_wiki.pk)


class ChildWikiNamingRaceTests(CampusTestCase):
    """The campus wiki is named by enrichment, which can land after the sweep."""

    def setUp(self) -> None:
        super().setUp()
        self.campus_wiki.name = "Unnamed Location in Poughkeepsie"
        self.campus_wiki.save(update_fields=["name"])
        records = _campus_records()
        records[0]["name"] = CAMPUS_NAME
        self.cache(records)

    def _child_wiki_names(self) -> set[str]:
        return {wiki.name for wiki in Wiki.objects.filter(parent_wiki=self.campus_wiki)}

    def test_a_building_named_like_the_campus_is_not_given_the_name_the_campus_is_about_to_take(self) -> None:
        self.location.official_name = CAMPUS_NAME
        self.location.save(update_fields=["official_name"])

        auto_nest_pin(self.pin)

        self.assertNotIn(CAMPUS_NAME, self._child_wiki_names())

    def test_names_given_before_the_campus_was_named_are_brought_up_to_date(self) -> None:
        auto_nest_pin(self.pin)
        Wiki.objects.filter(pk=self.campus_wiki.pk).update(name=CAMPUS_NAME)

        auto_nest_pin(self.pin)

        names = self._child_wiki_names()
        self.assertNotIn(CAMPUS_NAME, names)
        self.assertIn("Building 1", names, "the main building falls back to its number, not the campus name")
        self.assertIn("Building at Hudson River State Hospital", names)
        self.assertIn("Laundry", names, "a real name is left alone")


class MarkerPlacementTests(CampusTestCase):
    def test_a_building_whose_marker_is_taken_is_pinned_on_itself_or_not_at_all(self) -> None:
        """A U-shaped block's reported point is its courtyard; a pin there would never be matched back to it."""
        u_block = record(
            "osm:way/1",
            0,
            0,
            geometry=ring([(0, 60), (0, 120), (60, 120), (60, 100), (10, 100), (10, 80), (60, 80), (60, 60)]),
        )
        records = [u_block, record("osm:way/2", -150, -150, geometry=rect(-150, -150, 20, 20))]
        self.cache(records)
        from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings

        block = next(cluster for cluster in cluster_buildings(records) if "osm:way/1" in cluster.refs)
        taken = Location.objects.get_exact_or_create(block.latitude, block.longitude)[0]
        baker.make(Pin, profile=self.profile, location=taken, parent_pin=None, name="Already here")

        auto_nest_pin(self.pin)

        for child in self.pin.detail_pins.select_related("location"):
            latitude, longitude = float(child.location.latitude), float(child.location.longitude)
            self.assertTrue(
                block.covers(latitude, longitude) or meters_between(latitude, longitude, *offset(-150, -150)) < 20
            )
