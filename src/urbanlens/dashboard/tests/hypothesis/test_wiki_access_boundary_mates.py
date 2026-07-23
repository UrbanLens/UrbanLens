"""Tests for wiki_access.location_visible_to's boundary-mate matching.

Before this fix, a profile could only see a wiki by having a pin at the
EXACT SAME Location row the wiki points to. Nearly-identical coordinates
routinely resolve to distinct Location rows (the 50 m get_nearby_or_create
threshold vs. a larger generated building/property boundary), so a profile
whose pin genuinely sits on the same building - by the very same boundary
polygon the app already uses elsewhere to detect multi-location ambiguity
(see post_add_pin) - was denied access to that place's wiki entirely. This
locks in the fix: a pin at any Location whose own generated boundary polygon
contains the wiki's Location's point now also counts.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki_access import location_visible_to


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class LocationVisibleToBoundaryMateTests(TestCase):
    """A pin at a boundary-mate Location grants visibility, not just an exact match."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        # The wiki's own Location, at the center of a building-sized boundary.
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        Boundary.objects.create(location=self.wiki_location, generated_polygon=_square(-74.0, 40.0, 0.003))

    def test_exact_location_pin_is_visible(self) -> None:
        baker.make(Pin, profile=self.profile, location=self.wiki_location)
        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_no_pin_anywhere_is_not_visible(self) -> None:
        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_pin_at_a_different_location_within_the_same_boundary_is_visible(self) -> None:
        """The regression this fix closes: nearly-identical coordinates that
        resolved to a distinct Location row, but still fall inside the wiki
        location's own generated boundary polygon."""
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        self.assertNotEqual(nearby_location.pk, self.wiki_location.pk)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_pin_far_outside_the_boundary_is_not_visible(self) -> None:
        far_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        baker.make(Pin, profile=self.profile, location=far_location)

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_another_profiles_pin_at_a_boundary_mate_does_not_grant_visibility(self) -> None:
        """Boundary-mate matching still only counts the REQUESTING profile's own pins."""
        other = baker.make(User).profile
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        baker.make(Pin, profile=other, location=nearby_location)

        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_wiki_page_reachable_via_boundary_mate_pin(self) -> None:
        """End-to-end: the same boundary-mate pin unlocks the real wiki page."""
        self.client.force_login(self.user)
        wiki = baker.make(Wiki, location=self.wiki_location)
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        response = self.client.get(reverse("location.wiki", args=[wiki.location.slug]))

        self.assertEqual(response.status_code, 200)
