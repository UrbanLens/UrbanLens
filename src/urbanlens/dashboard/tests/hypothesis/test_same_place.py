"""``same_place`` and its set-based forms: one rule for "are these two pins the same place?"."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.pins.common_pins import common_pin_location_ids
from urbanlens.dashboard.services.pins.same_place import locations_sharing_a_place_with, same_place, same_place_q


def _profile():
    return baker.make("auth.User").profile


def _pin(profile, place: Place | None = None, *, location: Location | None = None) -> Pin:
    location = location or baker.make(Location, place=place)
    return baker.make(Pin, profile=profile, location=location, parent_pin=None)


class SamePlaceRuleTests(TestCase):
    def setUp(self):
        self.a = _profile()
        self.b = _profile()

    def test_two_points_on_one_building_are_the_same(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        self.assertTrue(same_place(_pin(self.a, building), _pin(self.b, building)))

    def test_two_buildings_in_one_parcel_are_not(self):
        parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        one = baker.make(Place, kind=PlaceKind.BUILDING, parent=parcel)
        two = baker.make(Place, kind=PlaceKind.BUILDING, parent=parcel)
        self.assertFalse(same_place(_pin(self.a, one), _pin(self.b, two)))

    def test_a_parcel_pin_and_a_building_pin_are_not(self):
        parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        self.assertFalse(same_place(_pin(self.a, parcel), _pin(self.b, building)))

    def test_placeless_pins_match_only_at_the_exact_location(self):
        shared = baker.make(Location, place=None)
        self.assertTrue(same_place(_pin(self.a, location=shared), _pin(self.b, location=shared)))
        self.assertFalse(same_place(_pin(self.a), _pin(self.b)))

    def test_a_placeless_pin_is_not_the_same_as_a_placed_one(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        self.assertFalse(same_place(_pin(self.a), _pin(self.b, building)))

    def test_split_siblings_are_not_the_same(self):
        old = baker.make(Place, kind=PlaceKind.PARCEL)
        left = baker.make(Place, kind=PlaceKind.PARCEL)
        right = baker.make(Place, kind=PlaceKind.PARCEL)
        self.assertNotEqual(left.pk, right.pk)
        self.assertNotEqual(old.pk, left.pk)
        self.assertFalse(same_place(_pin(self.a, left), _pin(self.b, right)))


class SamePlaceSetFormTests(TestCase):
    def setUp(self):
        self.a = _profile()
        self.b = _profile()

    def test_pins_in_common_is_unchanged_for_an_exact_location(self):
        shared = baker.make(Location, place=None)
        _pin(self.a, location=shared)
        _pin(self.b, location=shared)
        _pin(self.a)
        self.assertEqual(common_pin_location_ids([self.a, self.b]), {shared.pk})

    def test_pins_in_common_matches_two_points_on_one_building(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        mine = _pin(self.a, building)
        _pin(self.b, building)
        self.assertEqual(common_pin_location_ids([self.a, self.b]), {mine.location_id})
        self.assertEqual(len(common_pin_location_ids([self.b, self.a])), 1)

    def test_pins_in_common_ignores_a_parcel_pin_and_a_building_pin(self):
        _pin(self.a, baker.make(Place, kind=PlaceKind.PARCEL))
        _pin(self.b, baker.make(Place, kind=PlaceKind.BUILDING))
        self.assertEqual(common_pin_location_ids([self.a, self.b]), set())

    def test_results_are_the_callers_own_locations(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        mine = _pin(self.a, building)
        theirs = _pin(self.b, building)
        self.assertNotEqual(mine.location_id, theirs.location_id)
        self.assertNotIn(theirs.location_id, common_pin_location_ids([self.a, self.b]))

    def test_the_set_form_agrees_with_the_pairwise_form(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        other = baker.make(Place, kind=PlaceKind.BUILDING)
        mine = [_pin(self.a, building), _pin(self.a, other), _pin(self.a)]
        theirs = [_pin(self.b, building), _pin(self.b)]
        matched = set(
            Pin.objects.filter(same_place_q(Pin.objects.filter(profile=self.b)), profile=self.a).values_list(
                "pk", flat=True
            )
        )
        expected = {pin.pk for pin in mine if any(same_place(pin, t) for t in theirs)}
        self.assertEqual(matched, expected)
        self.assertEqual(expected, {mine[0].pk})

    def test_locations_sharing_a_place_with(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        _pin(self.a, building)
        here = baker.make(Location, place=building)
        elsewhere = baker.make(Location, place=baker.make(Place, kind=PlaceKind.BUILDING))
        self.assertEqual(locations_sharing_a_place_with(self.a, [here.pk, elsewhere.pk]), {here.pk})
        self.assertEqual(locations_sharing_a_place_with(self.a, []), set())

    def test_query_count_does_not_grow_with_pins(self):
        building = baker.make(Place, kind=PlaceKind.BUILDING)
        _pin(self.a, building)
        _pin(self.b, building)
        with CaptureQueriesContext(connection) as small:
            common_pin_location_ids([self.a, self.b])
        for _ in range(8):
            building = baker.make(Place, kind=PlaceKind.BUILDING)
            _pin(self.a, building)
            _pin(self.b, building)
        with CaptureQueriesContext(connection) as large:
            common_pin_location_ids([self.a, self.b])
        self.assertEqual(len(small), len(large))


def _load_checker():
    path = Path(__file__).resolve().parents[5] / "bin" / "check_same_place.py"
    spec = importlib.util.spec_from_file_location("check_same_place", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SamePlaceLintTests(SimpleTestCase):
    def setUp(self):
        self.checker = _load_checker()

    def test_flags_two_locations_compared_for_identity(self):
        source = "def f(a, b):\n    return a.location_id == b.location_id\n"
        self.assertEqual(len(self.checker.find_violations(source, "src/urbanlens/dashboard/services/x.py")), 1)

    def test_flags_a_filter_on_another_pins_locations(self):
        source = "def f(me, you):\n    return Pin.objects.filter(location_id__in=Pin.objects.filter(profile=me).values('location_id'))\n"
        self.assertEqual(len(self.checker.find_violations(source, "src/urbanlens/dashboard/services/x.py")), 1)

    def test_flags_a_radius_match_on_pins(self):
        source = "def f(p):\n    return Pin.objects.filter(location__point__dwithin=(p, D(m=5)))\n"
        self.assertEqual(len(self.checker.find_violations(source, "src/urbanlens/dashboard/services/x.py")), 1)

    def test_allowlisted_files_and_the_owner_pass(self):
        source = "def f(p):\n    return Pin.objects.filter(location__point__dwithin=(p, D(m=5)))\n"
        self.assertEqual(
            self.checker.find_violations(source, "src/urbanlens/dashboard/services/sharing/share_provenance.py"), []
        )
        self.assertEqual(self.checker.find_violations(source, self.checker.OWNER), [])

    def test_ordinary_location_filters_pass(self):
        source = "def f(loc, ids):\n    return Pin.objects.filter(location_id=loc.pk, location_id__in=ids)\n"
        self.assertEqual(self.checker.find_violations(source, "src/urbanlens/dashboard/services/x.py"), [])

    def test_the_tree_is_clean(self):
        self.assertEqual(self.checker.scan_tree(), [])
