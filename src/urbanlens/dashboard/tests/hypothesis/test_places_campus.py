"""Regression tests for the multi-building campus that started all this.

Hudson River State Hospital has 124 buildings. Importing them used to give
each new child pin its own Location, and each Location its own copy of the
*parcel* polygon - fetched by point lookup, which returns the parcel when you
ask about a building. Every point on the campus was then inside 125 boundaries
at once, and every visitor was told that 124 other locations covered their pin.

These tests pin the three symptoms that came from that one cause:

1. No competing places, however many buildings there are.
2. A building's page draws the building, not the 200-acre parcel.
3. One community page for the property, not one per coordinate.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.places.ambiguity import competing_wiki_locations
from urbanlens.dashboard.services.places.scope import effective_pin_type, place_polygon
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

#: Stands in for Hudson River's 124. The bug was independent of the count -
#: any number above one reproduced it - so this trades fidelity for a test
#: that finishes quickly.
CAMPUS_BUILDINGS = 12


def square(lng: float, lat: float, delta: float) -> MultiPolygon:
    """An axis-aligned square centred on a coordinate."""
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def make_place(
    kind: str,
    geometry: MultiPolygon | None,
    *,
    parent: Place | None = None,
    relation: str = PlaceRelation.PART_OF,
    name: str = "",
) -> Place:
    """Create a place with its derived columns filled, as provisioning would."""
    place = Place.objects.create(kind=kind, geometry=geometry, name=name)
    if geometry is not None:
        resolution.refresh_area(place)
    if parent is not None:
        lineage.set_parent(place, parent, relation)
    return place


class CampusTests(TestCase):
    """A parcel with many buildings behaves as one place, not many."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

        # A campus parcel, and a row of buildings strung across it.
        self.parcel = make_place(PlaceKind.PARCEL, square(-73.93, 41.73, 0.01), name="Hudson River State Hospital")
        self.buildings: list[Place] = []
        self.building_locations: list[Location] = []
        for index in range(CAMPUS_BUILDINGS):
            lng = -73.935 + (index * 0.0007)
            footprint = square(lng, 41.73, 0.0002)
            building = make_place(PlaceKind.BUILDING, footprint, parent=self.parcel, name=f"Building {index}")
            self.buildings.append(building)
            location = Location.objects.create(latitude=41.73, longitude=round(lng, 6))
            resolution.attach_location(location, building)
            self.building_locations.append(location)

        self.parcel.refresh_from_db()

        # Somebody's pin on the grounds, and the property's community page.
        self.lawn_location = Location.objects.create(latitude=41.7345, longitude=-73.9345)
        resolution.resolve_location_place(self.lawn_location)
        self.wiki = Wiki.objects.create(
            name="Hudson River State Hospital", location=self.lawn_location, place=self.parcel
        )

    def test_a_pin_on_the_campus_has_no_competing_places(self) -> None:
        """The reported bug: 124 buildings must not read as 124 rival places."""
        pin = baker.make(Pin, profile=self.profile, location=self.lawn_location)
        self.assertEqual(competing_wiki_locations(pin, self.profile), [])

    def test_a_pin_inside_a_building_has_no_competing_places_either(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.building_locations[3])
        self.assertEqual(competing_wiki_locations(pin, self.profile), [])

    def test_the_whole_campus_is_one_access_domain(self) -> None:
        self.assertEqual({place.domain_root_id for place in self.buildings}, {self.parcel.pk})

    def test_a_coordinate_resolves_to_the_building_it_stands_on(self) -> None:
        """Most-specific wins: inside a footprint is the building, not the parcel."""
        location = Location.objects.create(latitude=41.730149, longitude=-73.935149)
        self.assertEqual(resolution.resolve_location_place(location), self.buildings[0])

    def test_a_coordinate_on_the_grounds_resolves_to_the_parcel(self) -> None:
        self.assertEqual(self.lawn_location.place, self.parcel)

    def test_a_building_draws_its_own_footprint_not_the_parcel(self) -> None:
        building = self.buildings[0]
        self.assertEqual(place_polygon(building, BoundaryType.BUILDING), building.geometry)
        self.assertIsNone(place_polygon(building, BoundaryType.PROPERTY))

    def test_the_parcel_draws_the_grounds_and_no_single_building(self) -> None:
        self.assertEqual(place_polygon(self.parcel, BoundaryType.PROPERTY), self.parcel.geometry)
        self.assertIsNone(place_polygon(self.parcel, BoundaryType.BUILDING))

    def test_markers_commit_to_a_scope_on_a_multi_building_property(self) -> None:
        parcel_pin = baker.make(Pin, profile=self.profile, location=self.lawn_location)
        building_pin = baker.make(Pin, profile=self.profile, location=self.building_locations[1], parent_pin=parcel_pin)
        self.assertEqual(effective_pin_type(parcel_pin), PinType.PARCEL)
        self.assertEqual(effective_pin_type(building_pin), PinType.BUILDING)

    def test_a_pin_in_one_building_sees_the_property_wiki(self) -> None:
        """Pinning a building grants its parcel, without pinning every building."""
        baker.make(Pin, profile=self.profile, location=self.building_locations[5])
        self.assertTrue(location_visible_to(self.lawn_location, self.profile))

    def test_a_pin_on_the_grounds_sees_the_building_wikis(self) -> None:
        """The reverse: organising a property must not hide it from its own pinners."""
        building_wiki_location = Location.objects.create(latitude=41.7301, longitude=-73.9351)
        resolution.attach_location(building_wiki_location, self.buildings[2])
        Wiki.objects.create(name="Building 2", location=building_wiki_location, place=self.buildings[2])

        baker.make(Pin, profile=self.profile, location=self.lawn_location)
        self.assertTrue(location_visible_to(building_wiki_location, self.profile))

    def test_a_second_pinner_reaches_the_same_community_page(self) -> None:
        """Two people pinning one property metres apart share its wiki."""
        other_spot = Location.objects.create(latitude=41.7346, longitude=-73.9346)
        resolution.resolve_location_place(other_spot)
        self.assertEqual(Wiki.objects.get_for_location(other_spot), self.wiki)

    def test_a_pin_outside_the_campus_sees_nothing(self) -> None:
        far = Location.objects.create(latitude=42.5, longitude=-72.0)
        baker.make(Pin, profile=self.profile, location=far)
        self.assertFalse(location_visible_to(self.lawn_location, self.profile))


class OrdinaryHouseTests(TestCase):
    """One building on one parcel stays neutral - no scope is asserted."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.002))
        self.building = make_place(PlaceKind.BUILDING, square(-74.0, 40.0, 0.0004), parent=self.parcel)
        self.parcel.refresh_from_db()
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        resolution.resolve_location_place(self.location)

    def test_the_marker_type_stays_the_neutral_default(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.location)
        self.assertEqual(effective_pin_type(pin), PinType.LOCATION_MARKER)

    def test_both_outlines_are_still_drawn(self) -> None:
        """A house shows its footprint *and* its lot - neither is 'the wrong one'."""
        self.assertEqual(self.location.place, self.building)
        self.assertEqual(place_polygon(self.building, BoundaryType.BUILDING), self.building.geometry)
        self.assertEqual(place_polygon(self.building, BoundaryType.PROPERTY), self.parcel.geometry)
