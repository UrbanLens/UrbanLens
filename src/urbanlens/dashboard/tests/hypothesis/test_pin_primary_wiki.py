"""A pin's primary wiki comes from its subject place, never from access, and never from an inaccessible wiki (ADR34 section 5)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind, PlaceStatus
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pins.pin_edit import move_pin_to_coordinates
from urbanlens.dashboard.services.pins.primary_wiki import primary_wiki_for_pin, repoint_subjects
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.undo.handlers.pin import PinUndoHandler
from urbanlens.dashboard.services.undo.service import undo_latest

from .test_places_campus import make_place, square


def _location(lat: float, lng: float) -> Location:
    location = Location.objects.create(latitude=lat, longitude=lng)
    resolution.resolve_location_place(location)
    location.refresh_from_db()
    return location


class _Fixture(TestCase):
    """A parcel with one building on it, each with a wiki, and a viewer pinned on the building."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.003), name="Mill Parcel")
        self.building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0005), parent=self.parcel, name="Old Mill")
        self.parcel_location = _location(40.002, -74.002)
        self.building_location = _location(40.0, -74.0)
        self.assertEqual(self.building_location.place_id, self.building.pk)
        self.parcel_wiki = baker.make(Wiki, location=self.parcel_location, place=self.parcel, name="Parcel Wiki")
        self.building_wiki = baker.make(
            Wiki, location=self.building_location, place=self.building, name="Building Wiki"
        )
        self.pin = Pin.objects.create(profile=self.viewer, location=self.building_location, name="Mill")


class SubjectPlaceTests(_Fixture):
    def test_a_created_pin_takes_the_place_its_location_resolved_to(self) -> None:
        self.assertEqual(self.pin.subject_place_id, self.building.pk)

    def test_create_pin_for_profile_sets_the_subject(self) -> None:
        created = create_pin_for_profile(
            baker.make(User).profile, name="Via service", latitude=40.0001, longitude=-74.0001
        )
        self.assertEqual(created.pin.subject_place_id, self.building.pk)

    def test_move_then_undo_keeps_a_superseded_subject(self) -> None:
        self.building.status = PlaceStatus.SUPERSEDED
        self.building.save()
        move_pin_to_coordinates(self.pin, 40.002, -74.002)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.parcel.pk)
        undo_latest(self.viewer)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.building.pk)

    def test_a_deleted_pin_restores_its_subject(self) -> None:
        payload = PinUndoHandler.serialize([self.pin])
        self.assertEqual(payload[0]["subject_place_id"], self.building.pk)

    def test_a_moved_pin_is_about_the_place_it_moved_onto(self) -> None:
        move_pin_to_coordinates(self.pin, 40.002, -74.002)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.parcel.pk)

    def test_an_import_whose_place_resolves_later_gets_its_subject(self) -> None:
        elsewhere = _location(41.0, -73.0)
        imported = Pin.objects.create(profile=self.viewer, location=elsewhere, name="Imported")
        self.assertIsNone(imported.subject_place_id)
        later = make_place(PlaceKind.PARCEL, square(-73.0, 41.0, 0.001), name="Late Parcel")
        resolution.resolve_locations_in(later.geometry)
        imported.refresh_from_db()
        self.assertEqual(imported.subject_place_id, later.pk)

    def test_a_resolved_subject_is_not_overwritten_by_a_later_resolution(self) -> None:
        other = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0002), parent=self.parcel, name="Shed")
        resolution.resolve_locations_in(other.geometry)
        self.building_location.refresh_from_db()
        self.assertEqual(self.building_location.place_id, other.pk, "the location really did move onto the shed")
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.building.pk)


class PrimaryWikiTests(_Fixture):
    def test_the_subjects_wiki_is_primary_and_the_rest_are_related(self) -> None:
        wikis = primary_wiki_for_pin(self.pin, self.viewer)
        self.assertEqual(wikis.primary, self.building_wiki)
        self.assertEqual(wikis.related, [self.parcel_wiki])

    def test_a_subject_without_a_wiki_falls_back_to_the_nearest_ancestors(self) -> None:
        self.building_wiki.delete()
        self.assertEqual(primary_wiki_for_pin(self.pin, self.viewer).primary, self.parcel_wiki)

    def test_a_superseded_building_keeps_its_subject_and_its_wiki_stays_primary(self) -> None:
        self.building.status = PlaceStatus.SUPERSEDED
        self.building.save()
        replacement = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0006), parent=self.parcel, name="New Mill")
        resolution.resolve_locations_in(replacement.geometry)
        self.building_location.refresh_from_db()
        self.assertEqual(self.building_location.place_id, replacement.pk)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.building.pk)
        self.assertEqual(primary_wiki_for_pin(self.pin, self.viewer).primary, self.building_wiki)

    def test_repointing_moves_a_pin_to_the_successor_containing_its_point(self) -> None:
        self.building.status = PlaceStatus.SUPERSEDED
        self.building.save()
        replacement = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0006), parent=self.parcel, name="New Mill")
        self.assertEqual(repoint_subjects(self.building, [replacement]), 1)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, replacement.pk)

    def test_a_merge_with_several_survivors_is_refused_and_moves_nothing(self) -> None:
        first = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0006), parent=self.parcel, name="East Mill")
        second = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0007), parent=self.parcel, name="West Mill")
        with self.assertRaises(ValueError):
            repoint_subjects(self.building, [first, second], contained_only=False)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, self.building.pk)

    def test_a_merge_moves_every_pin_onto_the_one_survivor(self) -> None:
        survivor = make_place(PlaceKind.BUILDING, square(-60.0, 30.0, 0.001), parent=self.parcel, name="Far Mill")
        self.assertEqual(repoint_subjects(self.building, [self.building, survivor], contained_only=False), 1)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.subject_place_id, survivor.pk)

    def test_an_inaccessible_wiki_is_never_primary_even_if_it_is_the_subjects(self) -> None:
        stranger_parcel = make_place(PlaceKind.PARCEL, square(-60.0, 30.0, 0.001), name="Private Parcel")
        stranger_location = _location(30.0, -60.0)
        stranger_wiki = baker.make(Wiki, location=stranger_location, place=stranger_parcel, name="Hidden Wiki")
        Pin.objects.filter(pk=self.pin.pk).update(subject_place=stranger_parcel)
        self.pin.refresh_from_db()
        wikis = primary_wiki_for_pin(self.pin, self.viewer)
        self.assertNotEqual(wikis.primary, stranger_wiki)
        self.assertNotIn(stranger_wiki, wikis.related)

    def test_related_excludes_inaccessible_wikis(self) -> None:
        stranger_parcel = make_place(PlaceKind.PARCEL, square(-60.0, 30.0, 0.001), name="Private Parcel")
        baker.make(Wiki, location=_location(30.0, -60.0), place=stranger_parcel, name="Hidden Wiki")
        names = [wiki.name for wiki in primary_wiki_for_pin(self.pin, self.viewer).related]
        self.assertEqual(names, ["Parcel Wiki"])

    def test_a_placeless_pin_gets_its_exact_point_wiki(self) -> None:
        spot = _location(10.0, 10.0)
        self.assertIsNone(spot.place_id)
        wiki = baker.make(Wiki, location=spot, name="Spot Wiki")
        pin = Pin.objects.create(profile=self.viewer, location=spot, name="Spot")
        wikis = primary_wiki_for_pin(pin, self.viewer)
        self.assertEqual(wikis.primary, wiki)
        self.assertEqual(wikis.related, [])

    def test_a_stale_inaccessible_subject_still_yields_the_exact_point_wiki(self) -> None:
        stranger_parcel = make_place(PlaceKind.PARCEL, square(-60.0, 30.0, 0.001), name="Private Parcel")
        baker.make(Wiki, location=_location(30.0, -60.0), place=stranger_parcel, name="Hidden Wiki")
        self.building_wiki.delete()
        self.parcel_wiki.delete()
        exact = baker.make(Wiki, location=self.building_location, name="Point Wiki")
        Pin.objects.filter(pk=self.pin.pk).update(subject_place=stranger_parcel)
        self.pin.refresh_from_db()
        self.assertEqual(primary_wiki_for_pin(self.pin, self.viewer).primary, exact)

    def test_a_campus_building_without_a_place_beats_the_campus_wiki(self) -> None:
        self.building_wiki.delete()
        spot = _location(40.0004, -74.0004)
        self.assertEqual(spot.place_id, self.building.pk)
        pin = Pin.objects.create(profile=self.viewer, location=spot, name="Annex")
        point_wiki = baker.make(Wiki, location=spot, name="Annex Wiki")
        self.assertEqual(primary_wiki_for_pin(pin, self.viewer).primary, point_wiki)

    def test_primary_only_skips_the_related_list(self) -> None:
        wikis = primary_wiki_for_pin(self.pin, self.viewer, primary_only=True)
        self.assertEqual(wikis.primary, self.building_wiki)
        self.assertEqual(wikis.related, [])

    def test_the_pin_page_costs_a_bounded_number_of_queries(self) -> None:
        for index in range(8):
            baker.make(
                Wiki,
                location=_location(40.0 + index * 0.0001, -74.002),
                place=make_place(PlaceKind.BUILDING, square(-74.0, 40.0 + index * 0.0001, 0.00001), parent=self.parcel),
                name=f"B{index}",
            )
        with CaptureQueriesContext(connection) as queries:
            primary_wiki_for_pin(self.pin, self.viewer)
        self.assertLess(len(queries), 16)

    def test_the_detail_payload_names_the_primary_and_keeps_the_pins_own_location_slug(self) -> None:
        payload = build_pin_detail(self.pin, self.viewer)
        self.assertEqual(payload["location_slug"], self.building_location.slug)
        self.assertEqual(payload["primary_wiki_location_slug"], self.building_wiki.location.slug or None)
        self.assertEqual([row["name"] for row in payload["related_wikis"]], ["Parcel Wiki"])

    def test_a_placeless_pin_with_no_wiki_has_none(self) -> None:
        pin = Pin.objects.create(profile=self.viewer, location=_location(11.0, 11.0), name="Bare")
        self.assertIsNone(primary_wiki_for_pin(pin, self.viewer).primary)
