"""``same_place`` and its set-based forms: one rule for "are these two pins the same place?"."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.eligibility import eligible_wikis
from urbanlens.dashboard.services.pins.common_pins import common_pin_location_ids
from urbanlens.dashboard.services.pins.same_place import (
    locations_sharing_a_place_with,
    same_place,
    same_place_location_ids,
    same_place_q,
)
from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids
from urbanlens.dashboard.tests.hypothesis.place_helpers import make_place


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


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class RealPlaceRowTests(TestCase):
    """A parcel with two buildings PART_OF it, built the way provisioning builds them (``domain_root`` set)."""

    def setUp(self):
        self.a = _profile()
        self.b = _profile()
        self.parcel = make_place(PlaceKind.PARCEL, _square(-73.9, 40.7, 0.0005))
        self.one = make_place(PlaceKind.BUILDING, _square(-73.9003, 40.7, 0.0001), parent=self.parcel)
        self.two = make_place(PlaceKind.BUILDING, _square(-73.8997, 40.7, 0.0001), parent=self.parcel)

    def test_the_fixture_shares_a_domain_root(self):
        self.assertEqual(self.one.domain_root_id, self.parcel.pk)
        self.assertEqual(self.two.domain_root_id, self.parcel.pk)

    def test_two_buildings_in_one_parcel_are_not_the_same(self):
        self.assertFalse(same_place(_pin(self.a, self.one), _pin(self.b, self.two)))

    def test_a_parcel_pin_and_a_building_pin_are_not_the_same(self):
        self.assertFalse(same_place(_pin(self.a, self.parcel), _pin(self.b, self.one)))

    def test_the_same_building_is_the_same(self):
        self.assertTrue(same_place(_pin(self.a, self.one), _pin(self.b, self.one)))

    def test_set_form_keeps_buildings_in_a_parcel_apart(self):
        _pin(self.a, self.one)
        _pin(self.b, self.two)
        _pin(self.b, self.parcel)
        self.assertEqual(common_pin_location_ids([self.a, self.b]), set())

    def test_split_siblings_are_not_the_same(self):
        left = make_place(PlaceKind.PARCEL, _square(-73.8, 40.6, 0.0004))
        right = make_place(PlaceKind.PARCEL, _square(-73.7, 40.6, 0.0004))
        self.assertFalse(same_place(_pin(self.a, left), _pin(self.b, right)))

    def test_a_superseded_place_falls_back_to_the_exact_location(self):
        Place.objects.filter(pk=self.one.pk).update(status=PlaceStatus.SUPERSEDED)
        mine, theirs = _pin(self.a, self.one), _pin(self.b, self.one)
        self.assertFalse(same_place(mine, theirs))
        self.assertEqual(common_pin_location_ids([self.a, self.b]), set())
        shared = Location.objects.get(pk=mine.location_id)
        self.assertTrue(same_place(mine, _pin(self.b, location=shared)))

    def test_an_implausibly_large_place_does_not_join_strangers(self):
        huge = make_place(PlaceKind.BUILDING, _square(-70.0, 40.0, 0.05))
        self.assertTrue(Place.objects.implausible().filter(pk=huge.pk).exists())
        self.assertFalse(same_place(_pin(self.a, huge), _pin(self.b, huge)))
        self.assertEqual(common_pin_location_ids([self.a, self.b]), set())

    def test_set_form_with_a_placeless_side(self):
        placeless = _pin(self.a)
        _pin(self.b)  # b's own placeless pin sits on a different Location: null places must not match each other
        _pin(self.b, self.one)
        self.assertEqual(
            set(same_place_location_ids(Pin.objects.filter(profile=self.b)).values_list("pk", flat=True))
            & {placeless.location_id},
            set(),
        )
        self.assertEqual(locations_sharing_a_place_with(self.b, {placeless.location_id}), set())

    def test_set_form_superseded_and_implausible_are_excluded(self):
        huge = make_place(PlaceKind.BUILDING, _square(-70.0, 40.0, 0.05))
        Place.objects.filter(pk=self.two.pk).update(status=PlaceStatus.SUPERSEDED)
        mine = [_pin(self.a, p).location_id for p in (self.one, self.two, huge)]
        for p in (self.one, self.two, huge):
            _pin(self.b, p)
        self.assertEqual(locations_sharing_a_place_with(self.b, set(mine)), {mine[0]})

    def test_eligibility_widens_to_the_same_place_and_stays_visible(self):
        visited = baker.make(Location, place=self.one)
        beside = baker.make(Location, place=self.one)
        other_building = baker.make(Location, place=self.two)
        unseen = baker.make(Location, place=self.one)  # on the place, not pinned by a
        baker.make(Pin, profile=self.a, location=visited, parent_pin=None, last_visited=timezone.now())
        baker.make(Pin, profile=self.a, location=beside, parent_pin=None)
        baker.make(Pin, profile=self.a, location=other_building, parent_pin=None)
        for location in (visited, beside, other_building, unseen):
            baker.make(Wiki, location=location)
        eligible = set(eligible_wikis(self.a).values_list("location_id", flat=True))
        # `unseen` is eligible because wiki access is by domain: holding a pin on the place makes its Locations' wikis visible.
        self.assertEqual(eligible, {visited.pk, beside.pk, unseen.pk})
        self.assertNotIn(other_building.pk, eligible)

    def test_profile_shared_visited_lists_only_the_viewers_location(self):
        mine, theirs = _pin(self.a, self.one), _pin(self.b, self.one)
        _pin(self.b, self.two)
        for pin in (mine, theirs):
            Pin.objects.filter(pk=pin.pk).update(last_visited=timezone.now())
        their_visited = Pin.objects.filter(profile=self.b).visited()
        got = set(
            Pin.objects.filter(profile=self.a)
            .visited()
            .filter(same_place_q(their_visited))
            .values_list("location_id", flat=True)
        )
        self.assertEqual(got, {mine.location_id})

    def test_trip_visibility_uses_the_same_rule(self):
        Profile.objects.filter(pk=self.b.pk).update(trip_pin_location_visibility="common_pin")
        self.b.refresh_from_db()
        trip = baker.make(Trip, creator=self.b)
        on_one = baker.make(
            TripActivity,
            trip=trip,
            location=baker.make(Location, place=self.one),
            added_by=self.b,
            location_hidden=False,
        )
        on_two = baker.make(
            TripActivity,
            trip=trip,
            location=baker.make(Location, place=self.two),
            added_by=self.b,
            location_hidden=False,
        )
        _pin(self.a, self.one)
        hidden = viewer_hidden_activity_ids([on_one, on_two], self.a)
        self.assertNotIn(on_one.id, hidden)
        self.assertIn(on_two.id, hidden)


def _load_checker():
    path = Path(__file__).resolve().parents[5] / "bin" / "check_same_place.py"
    spec = importlib.util.spec_from_file_location("check_same_place", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SRC = "src/urbanlens/dashboard/services/x.py"


class SamePlaceLintTests(SimpleTestCase):
    def setUp(self):
        self.checker = _load_checker()

    def _found(self, source: str, path: str = SRC) -> int:
        return len(self.checker.find_violations(source, path))

    def test_flags_two_locations_compared_for_identity(self):
        self.assertEqual(self._found("def f(a, b):\n    return a.location_id == b.location_id\n"), 1)

    def test_flags_the_three_base_branch_shapes(self):
        userprofile = (
            "def f(mine, theirs):\n    return mine.pins.filter(location__in=theirs.pins.values('location_id'))\n"
        )
        trip_visibility = "def f(viewer, place_ids, loc_ids):\n    at = Q(location__place_id__in=place_ids) | Q(location_id__in=loc_ids)\n    return Pin.objects.filter(at, profile=viewer)\n"
        eligibility = "def f(visited):\n    return Wiki.objects.filter(location_id__in=visited.values_list('location_id', flat=True))\n"
        self.assertGreaterEqual(self._found(userprofile), 1)
        self.assertGreaterEqual(self._found(trip_visibility), 1)
        self.assertGreaterEqual(self._found(eligibility), 1)

    def test_flags_the_probes(self):
        probes = {
            "attribute": "def f(pin):\n    return Pin.objects.filter(location=pin.location)\n",
            "in_ids": "def f(ids):\n    return Pin.objects.filter(location__in=ids)\n",
            "pk_in": "def f(ids):\n    return Pin.objects.filter(location__pk__in=ids)\n",
            "exists": "def f():\n    return Exists(Pin.objects.filter(location_id=OuterRef('location_id')))\n",
            "q_dict": "def f(key, ids):\n    return Q(**{'location_id__in': ids})\n",
            "dwithin": "def f(p):\n    return Pin.objects.filter(location__point__dwithin=(p, D(m=5)))\n",
            "distance": "def f(p):\n    return Pin.objects.filter(location__point__distance_lte=(p, D(m=5)))\n",
            "intersect": "def f(a, b):\n    return set(a.values_list('location_id', flat=True)) & set(b.values_list('location_id', flat=True))\n",
        }
        for name, source in probes.items():
            with self.subTest(name):
                self.assertGreaterEqual(self._found(source), 1)

    def test_a_waiver_comment_clears_the_line(self):
        source = "def f(ids):\n    return Pin.objects.filter(location__in=ids)  # same-place: exact match is intended\n"
        self.assertEqual(self._found(source), 0)

    def test_a_trailing_waiver_does_not_cover_the_next_statement(self):
        source = "def f(x, y):\n    a = Pin.objects.filter(location=x)  # same-place: intended\n    b = Pin.objects.filter(location=y)\n    return a, b\n"
        self.assertEqual(self._found(source), 1)

    def test_a_comment_line_above_waives_the_statement(self):
        source = "def f(x):\n    # same-place: intended\n    return Pin.objects.filter(location=x)\n"
        self.assertEqual(self._found(source), 0)

    def test_radius_modules_and_the_owner_pass(self):
        source = "def f(p):\n    return Pin.objects.filter(location__point__dwithin=(p, D(m=5)))\n"
        self.assertEqual(self._found(source, "src/urbanlens/dashboard/services/sharing/share_provenance.py"), 0)
        self.assertEqual(self._found(source, self.checker.OWNER), 0)
        self.assertEqual(self._found(source), 1)

    def test_literals_and_unrelated_filters_pass(self):
        self.assertEqual(
            self._found("def f():\n    return Pin.objects.filter(location__isnull=False, location__name='x')\n"), 0
        )
        self.assertEqual(self._found("def f(u, ids):\n    return Review.objects.filter(user=u, id__in=ids)\n"), 0)

    def test_the_tree_is_clean(self):
        self.assertEqual(self.checker.scan_tree(), [])
