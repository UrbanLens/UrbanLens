"""Tests for the wiki page's "up to the parent wiki" link.

Wikis nest themselves - a building's wiki becomes a child of the campus's -
so a nested page needs a way up. The link is gated on
``wiki_access.visible_parent_wiki`` rather than rendered from
``wiki.parent_wiki`` directly, because a breadcrumb to a page the viewer would
get a 404 from is itself a disclosure: it confirms a place exists that they
have not earned.

Within one access domain (a building ``PART_OF`` its parcel) the parent is
always reachable, so the interesting case is the ``MEMBER_OF`` edge: a campus
made of several parcels is earned only by holding *every* member, so someone
who pinned one parcel must not be shown the campus above it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.wiki.wiki_access import visible_parent_wiki

from .test_places_campus import make_place, square


def _location_on(place, *, lat: float, lng: float) -> Location:
    location = Location.objects.create(latitude=round(lat, 6), longitude=round(lng, 6))
    resolution.attach_location(location, place)
    return location


def _wiki_on(location: Location, name: str, *, parent: Wiki | None = None) -> Wiki:
    return baker.make(Wiki, location=location, name=name, parent_wiki=parent, officially_created=True)


class VisibleParentWikiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def test_a_parent_inside_the_same_domain_is_offered(self) -> None:
        """A building sits PART_OF its parcel, so holding either holds both."""
        parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.001), parent=parcel, relation=PlaceRelation.PART_OF)

        # Distinct coordinates: Location is unique on (latitude, longitude).
        # The building's point still sits inside both squares.
        parcel_location = _location_on(parcel, lat=40.005, lng=-74.005)
        building_location = _location_on(building, lat=40.0, lng=-74.0)
        parent_wiki = _wiki_on(parcel_location, "Hudson River State Hospital")
        child_wiki = _wiki_on(building_location, "Powerhouse", parent=parent_wiki)

        baker.make(Pin, profile=self.profile, location=building_location)

        self.assertEqual(visible_parent_wiki(child_wiki, self.profile), parent_wiki)

    def test_an_unearned_aggregate_parent_is_withheld(self) -> None:
        """Holding one parcel of a multi-parcel campus does not earn the campus,
        so its wiki must not be named on the parcel's page."""
        campus = make_place(PlaceKind.PARCEL, None)
        parcel_a = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=campus, relation=PlaceRelation.MEMBER_OF)
        make_place(PlaceKind.PARCEL, square(-73.0, 40.0, 0.01), parent=campus, relation=PlaceRelation.MEMBER_OF)

        campus_location = _location_on(campus, lat=40.5, lng=-73.5)
        parcel_location = _location_on(parcel_a, lat=40.0, lng=-74.0)
        campus_wiki = _wiki_on(campus_location, "The Whole Campus")
        parcel_wiki = _wiki_on(parcel_location, "North Parcel", parent=campus_wiki)

        baker.make(Pin, profile=self.profile, location=parcel_location)

        self.assertIsNone(visible_parent_wiki(parcel_wiki, self.profile))

    def test_a_root_wiki_has_no_parent(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        location = _location_on(parcel, lat=40.0, lng=-74.0)
        self.assertIsNone(visible_parent_wiki(_wiki_on(location, "Standalone"), self.profile))


class WikiPageParentLinkTests(TestCase):
    """The rendered page: the link appears only when the parent is visible."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _render_child_page(self, *, same_domain: bool):
        if same_domain:
            parent_place = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
            child_place = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.001), parent=parent_place, relation=PlaceRelation.PART_OF)
            parent_location = _location_on(parent_place, lat=40.005, lng=-74.005)
        else:
            parent_place = make_place(PlaceKind.PARCEL, None)
            child_place = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), parent=parent_place, relation=PlaceRelation.MEMBER_OF)
            make_place(PlaceKind.PARCEL, square(-73.0, 40.0, 0.01), parent=parent_place, relation=PlaceRelation.MEMBER_OF)
            parent_location = _location_on(parent_place, lat=40.5, lng=-73.5)

        child_location = _location_on(child_place, lat=40.0, lng=-74.0)
        parent_wiki = _wiki_on(parent_location, "Parent Place Name")
        _wiki_on(child_location, "Child Place Name", parent=parent_wiki)
        baker.make(Pin, profile=self.profile, location=child_location)

        return self.client.get(reverse("location.wiki", args=[child_location.slug]))

    def test_a_visible_parent_is_linked(self) -> None:
        response = self._render_child_page(same_domain=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parent Place Name")

    def test_an_unearned_parent_is_not_named_on_the_page(self) -> None:
        """Not merely unlinked - its name must not reach the response at all."""
        response = self._render_child_page(same_domain=False)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Parent Place Name")
