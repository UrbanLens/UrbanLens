"""Property tests for the place access predicate.

The predicate is small but its consequences are not, so these assert the
properties rather than a handful of examples:

- **Symmetry within a domain.** A parcel and everything ``PART_OF`` it is one
  access domain; a pin anywhere in it reaches every wiki in it, in either
  direction. Splitting a property into buildings must never change who can see
  what.
- **All members, for aggregates.** A ``MEMBER_OF`` parent is reachable only by
  holding every one of its members, and holding all-but-one is not enough.
- **Earning is recursive.** Completing one tier can complete the tier above it.
- **Nothing user-drawn ever counts.** The ``Boundary`` table is where every
  community drawing lives, and the predicate must not read it.
- **Superseded geometry never grants.** The old campus outline still contains
  every post-split pin; containment against it must resolve to nothing.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant, PlaceKind, PlaceRelation, PlaceStatus
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.wiki.wiki_access import accessible_domain_ids, place_visible_to

from .test_places_campus import make_place, square

SLOW_DB_TEST = settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def pin_on(profile, place: Place, *, lat: float, lng: float) -> Pin:
    """Give a profile a pin standing on a place."""
    location = Location.objects.create(latitude=round(lat, 6), longitude=round(lng, 6))
    resolution.attach_location(location, place)
    return baker.make(Pin, profile=profile, location=location)


class DomainSymmetryTests(TestCase):
    """Within one PART_OF tree, access is indivisible."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))

    @given(st.integers(min_value=1, max_value=6), st.integers(min_value=0, max_value=5))
    @SLOW_DB_TEST
    def test_a_pin_anywhere_in_the_tree_reaches_all_of_it(self, building_count: int, pinned_index: int) -> None:
        Pin.objects.filter(profile=self.profile).delete()
        Place.objects.filter(parent=self.parcel).delete()

        buildings = [make_place(PlaceKind.BUILDING, square(-74.0 + i * 0.001, 40.0, 0.0002), parent=self.parcel) for i in range(building_count)]
        target = buildings[pinned_index % building_count]
        pin_on(self.profile, target, lat=40.0, lng=-74.0 + (pinned_index % building_count) * 0.001)

        # Upward, downward, and sideways - all one domain.
        self.assertTrue(place_visible_to(self.parcel, self.profile))
        for building in buildings:
            self.assertTrue(place_visible_to(building, self.profile))

    def test_a_pin_on_the_parcel_reaches_its_buildings(self) -> None:
        building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0002), parent=self.parcel)
        pin_on(self.profile, self.parcel, lat=40.005, lng=-74.005)
        self.assertTrue(place_visible_to(building, self.profile))

    def test_an_unrelated_parcel_is_not_reachable(self) -> None:
        other = make_place(PlaceKind.PARCEL, square(-71.0, 43.0, 0.01))
        pin_on(self.profile, self.parcel, lat=40.0, lng=-74.0)
        self.assertFalse(place_visible_to(other, self.profile))


class AggregateEarningTests(TestCase):
    """A MEMBER_OF parent needs every member, and earning is recursive."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(st.integers(min_value=2, max_value=5), st.integers(min_value=0, max_value=5))
    @SLOW_DB_TEST
    def test_all_but_one_member_is_never_enough(self, member_count: int, held: int) -> None:
        Pin.objects.filter(profile=self.profile).delete()
        Place.objects.all().delete()

        site = make_place(PlaceKind.SITE, None)
        members = [make_place(PlaceKind.PARCEL, square(-74.0 + i * 0.05, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF) for i in range(member_count)]
        site.refresh_from_db()

        held = min(held, member_count)
        for index in range(held):
            pin_on(self.profile, members[index], lat=40.0, lng=-74.0 + index * 0.05)

        self.assertEqual(place_visible_to(site, self.profile), held == member_count)

    def test_earning_one_tier_can_complete_the_next(self) -> None:
        """campus -> {A, B}; A -> {A1, A2}. Pins in A1, A2 and B earn the campus."""
        campus = make_place(PlaceKind.SITE, None)
        parcel_a = make_place(PlaceKind.PARCEL, None, parent=campus, relation=PlaceRelation.MEMBER_OF)
        parcel_b = make_place(PlaceKind.PARCEL, square(-73.0, 40.0, 0.01), parent=campus, relation=PlaceRelation.MEMBER_OF)
        sub_1 = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.005), parent=parcel_a, relation=PlaceRelation.MEMBER_OF)
        sub_2 = make_place(PlaceKind.PARCEL, square(-74.02, 40.0, 0.005), parent=parcel_a, relation=PlaceRelation.MEMBER_OF)

        pin_on(self.profile, sub_1, lat=40.0, lng=-74.0)
        self.assertFalse(place_visible_to(parcel_a, self.profile))
        self.assertFalse(place_visible_to(campus, self.profile))

        pin_on(self.profile, sub_2, lat=40.0, lng=-74.02)
        self.assertTrue(place_visible_to(parcel_a, self.profile))
        self.assertFalse(place_visible_to(campus, self.profile))

        pin_on(self.profile, parcel_b, lat=40.0, lng=-73.0)
        self.assertTrue(place_visible_to(campus, self.profile))

    def test_an_aggregate_is_never_resolved_onto_directly(self) -> None:
        """Strict earning must not be bypassable by pinning the site's own outline."""
        site = make_place(PlaceKind.SITE, square(-74.0, 40.0, 0.05))
        make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        make_place(PlaceKind.PARCEL, square(-74.03, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        site.refresh_from_db()

        # A point inside the site but outside every member.
        self.assertIsNone(Place.objects.resolve_for_point(40.04, -74.04))

    def test_a_grant_is_enough_on_its_own(self) -> None:
        site = make_place(PlaceKind.SITE, None)
        make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        make_place(PlaceKind.PARCEL, square(-73.0, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        site.refresh_from_db()

        self.assertFalse(place_visible_to(site, self.profile))
        PlaceAccessGrant.objects.create(profile=self.profile, place=site, reason=GrantReason.GRANDFATHERED_BACKFILL)
        self.assertTrue(place_visible_to(site, self.profile))


class AntiGamingTests(TestCase):
    """Nothing a user can draw or own may widen what they see."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.target = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.001))
        self.elsewhere = Location.objects.create(latitude=10.0, longitude=10.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.elsewhere)

    def _huge(self) -> MultiPolygon:
        ring = ((-180.0, -85.0), (180.0, -85.0), (180.0, 85.0), (-180.0, 85.0), (-180.0, -85.0))
        return MultiPolygon(Polygon(ring, srid=4326), srid=4326)

    def test_a_pin_owned_boundary_grants_nothing(self) -> None:
        Boundary.objects.create(pin=self.pin, profile=self.profile, boundary_type=BoundaryType.PROPERTY, polygon=self._huge(), generated_polygon=self._huge())
        self.assertFalse(place_visible_to(self.target, self.profile))

    def test_a_community_drawn_wiki_boundary_grants_nothing(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki = Wiki.objects.create(name="Anywhere", location=self.elsewhere)
        Boundary.objects.create(wiki=wiki, boundary_type=BoundaryType.PROPERTY, polygon=self._huge())
        self.assertFalse(place_visible_to(self.target, self.profile))

    def test_another_profiles_pin_grants_nothing(self) -> None:
        stranger = baker.make(User).profile
        pin_on(stranger, self.target, lat=40.0, lng=-74.0)
        self.assertFalse(place_visible_to(self.target, self.profile))


class SupersessionTests(TestCase):
    """Historical geometry keeps its shape but loses its power."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.old_campus = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.05))
        self.new_a = make_place(PlaceKind.PARCEL, square(-74.02, 40.0, 0.01), parent=self.old_campus, relation=PlaceRelation.MEMBER_OF)
        self.new_b = make_place(PlaceKind.PARCEL, square(-73.98, 40.0, 0.01), parent=self.old_campus, relation=PlaceRelation.MEMBER_OF)
        Place.objects.filter(pk=self.old_campus.pk).update(status=PlaceStatus.SUPERSEDED)
        self.old_campus.refresh_from_db()

    def test_superseded_geometry_never_resolves(self) -> None:
        """A point inside the old campus but outside both successors resolves to nothing."""
        self.assertIsNone(Place.objects.resolve_for_point(40.04, -74.04))

    def test_holding_one_successor_does_not_grant_the_old_campus(self) -> None:
        pin_on(self.profile, self.new_a, lat=40.0, lng=-74.02)
        self.assertTrue(place_visible_to(self.new_a, self.profile))
        self.assertFalse(place_visible_to(self.old_campus, self.profile))

    def test_holding_every_successor_earns_the_old_campus(self) -> None:
        pin_on(self.profile, self.new_a, lat=40.0, lng=-74.02)
        pin_on(self.profile, self.new_b, lat=40.0, lng=-73.98)
        self.assertTrue(place_visible_to(self.old_campus, self.profile))


class ReachabilityTests(TestCase):
    """No place is ever stranded: every one is reachable by some set of pins."""

    def test_a_geometry_less_building_is_reachable_through_its_parcel(self) -> None:
        profile = baker.make(User).profile
        parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        unlocatable = make_place(PlaceKind.BUILDING, None, parent=parcel)

        pin_on(profile, parcel, lat=40.0, lng=-74.0)
        self.assertTrue(place_visible_to(unlocatable, profile))

    def test_a_placeless_location_is_still_reachable_by_an_exact_pin(self) -> None:
        from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

        profile = baker.make(User).profile
        nowhere = Location.objects.create(latitude=12.3456, longitude=65.4321)
        baker.make(Pin, profile=profile, location=nowhere)
        self.assertTrue(location_visible_to(nowhere, profile))

    def test_an_empty_profile_reaches_nothing(self) -> None:
        profile = baker.make(User).profile
        self.assertEqual(accessible_domain_ids(profile), set())


class LineageIntegrityTests(TestCase):
    """The denormalised domain column has to stay exact, or access goes wrong."""

    def test_re_parenting_repropagates_the_domain_through_the_subtree(self) -> None:
        first = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        second = make_place(PlaceKind.PARCEL, square(-73.0, 40.0, 0.01))
        building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0002), parent=first)
        wing = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0001), parent=building)

        self.assertEqual(Place.objects.get(pk=wing.pk).domain_root_id, first.pk)
        lineage.set_parent(building, second, PlaceRelation.PART_OF)
        self.assertEqual(Place.objects.get(pk=wing.pk).domain_root_id, second.pk)

    def test_a_member_of_edge_stops_the_domain_from_propagating(self) -> None:
        site = make_place(PlaceKind.SITE, None)
        parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0002), parent=parcel)

        self.assertEqual(Place.objects.get(pk=building.pk).domain_root_id, parcel.pk)
        self.assertNotEqual(Place.objects.get(pk=parcel.pk).domain_root_id, site.pk)

    def test_a_cycle_is_refused(self) -> None:
        parent = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        child = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0002), parent=parent)
        with self.assertRaises(lineage.PlaceLineageError):
            lineage.set_parent(parent, child, PlaceRelation.PART_OF)

    def test_attaching_a_member_makes_the_parent_an_aggregate(self) -> None:
        site = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.05))
        self.assertFalse(Place.objects.get(pk=site.pk).is_aggregate)
        make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=site, relation=PlaceRelation.MEMBER_OF)
        self.assertTrue(Place.objects.get(pk=site.pk).is_aggregate)
