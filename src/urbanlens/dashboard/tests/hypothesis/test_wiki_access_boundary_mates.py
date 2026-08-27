"""Tests for wiki_access.location_visible_to's "same place" matching.

Originally, a profile could only see a wiki by having a pin at the EXACT SAME
Location row the wiki pointed to. Nearly-identical coordinates routinely
resolve to distinct Location rows, so a profile whose pin genuinely sat on the
same building was denied access to that place's wiki entirely.

The fix used to be expressed as containment against the wiki location's own
copy of the boundary polygon ("boundary mates"). Since the Place model landed,
it is expressed directly: two coordinates that resolve onto the same
real-world thing share its access domain, so no polygon comparison happens at
read time at all. These tests keep the original scenarios and assert the same
outcomes through the new mechanism - including the anti-gaming invariants,
which are now structural (the access predicate does not read the ``Boundary``
table at all).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

from .test_places_campus import make_place, square as _square


class LocationVisibleToSamePlaceTests(TestCase):
    """A pin anywhere on the same place grants visibility, not just an exact match."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        # A building-sized parcel, and the wiki's own Location at its centre.
        self.parcel = make_place(PlaceKind.PARCEL, _square(-74.0, 40.0, 0.003))
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        resolution.resolve_location_place(self.wiki_location)

    def test_exact_location_pin_is_visible(self) -> None:
        baker.make(Pin, profile=self.profile, location=self.wiki_location)
        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_no_pin_anywhere_is_not_visible(self) -> None:
        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_pin_at_a_different_location_on_the_same_place_is_visible(self) -> None:
        """The regression this closes: nearly-identical coordinates that resolve
        to a distinct Location row, but stand on the same real-world thing."""
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        resolution.resolve_location_place(nearby_location)
        self.assertNotEqual(nearby_location.pk, self.wiki_location.pk)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_pin_far_outside_the_place_is_not_visible(self) -> None:
        far_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        resolution.resolve_location_place(far_location)
        baker.make(Pin, profile=self.profile, location=far_location)

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_another_profiles_pin_on_the_same_place_does_not_grant_visibility(self) -> None:
        """Matching still only counts the REQUESTING profile's own pins."""
        other = baker.make(User).profile
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        resolution.resolve_location_place(nearby_location)
        baker.make(Pin, profile=other, location=nearby_location)

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_a_user_drawn_polygon_does_not_grant_visibility(self) -> None:
        """The anti-gaming invariant, now structural: the predicate never reads
        the Boundary table, which is where every drawn shape lives."""
        far_location = Location.objects.create(latitude=40.5, longitude=-74.5)
        resolution.resolve_location_place(far_location)
        baker.make(Pin, profile=self.profile, location=far_location)
        Boundary.objects.create(location=self.wiki_location, polygon=_square(-74.0, 40.0, 1.0))

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_a_pin_owned_boundary_row_does_not_widen_visibility(self) -> None:
        far_location = Location.objects.create(latitude=40.5, longitude=-74.5)
        resolution.resolve_location_place(far_location)
        far_pin = baker.make(Pin, profile=self.profile, location=far_location)
        Boundary.objects.create(location=self.wiki_location, pin=far_pin, profile=self.profile, generated_polygon=_square(-74.0, 40.0, 1.0))

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_a_pin_on_a_building_of_the_place_counts(self) -> None:
        """A point inside a building footprint is on that building's grounds,
        and the two share one access domain."""
        building = make_place(PlaceKind.BUILDING, _square(-74.001, 40.001, 0.0002), parent=self.parcel)
        inside_building = Location.objects.create(latitude=40.001, longitude=-74.001)
        resolution.resolve_location_place(inside_building)
        self.assertEqual(inside_building.place, building)
        baker.make(Pin, profile=self.profile, location=inside_building)

        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_building_boundary_type_still_resolves_for_display(self) -> None:
        """Unrelated to access, but the two used to share one mechanism."""
        building = make_place(PlaceKind.BUILDING, _square(-74.001, 40.001, 0.0002), parent=self.parcel)
        self.assertEqual(Boundary.objects.official_polygons_by_location_id([self.wiki_location.pk], BoundaryType.PROPERTY)[self.wiki_location.pk], self.parcel.geometry)
        self.assertIsNotNone(building.geometry)

    def test_wiki_page_reachable_via_same_place_pin(self) -> None:
        """End-to-end: a pin on the same place unlocks the real wiki page."""
        self.client.force_login(self.user)
        wiki = baker.make(Wiki, location=self.wiki_location, place=self.parcel)
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        resolution.resolve_location_place(nearby_location)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        response = self.client.get(reverse("location.wiki", args=[wiki.location.slug]))

        self.assertEqual(response.status_code, 200)

    def test_a_second_pinners_own_slug_reaches_the_page(self) -> None:
        """Everyone who pinned one property reaches it from their own URL."""
        self.client.force_login(self.user)
        baker.make(Wiki, location=self.wiki_location, place=self.parcel)
        mine = Location.objects.create(latitude=40.0008, longitude=-74.0008)
        resolution.resolve_location_place(mine)
        baker.make(Pin, profile=self.profile, location=mine)
        mine.ensure_slug()

        response = self.client.get(reverse("location.wiki", args=[mine.slug]))

        self.assertEqual(response.status_code, 200)
