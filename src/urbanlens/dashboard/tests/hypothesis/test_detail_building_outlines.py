"""P222: a pin's map draws every building child's own outline while child pin details are on, and none while off."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki

from .test_places_campus import make_place


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _Campus(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.parcel = make_place(PlaceKind.PARCEL, _square(-73.928, 41.733, 0.004))
        campus_location = baker.make(Location, latitude="41.733000", longitude="-73.928000", place=self.parcel)
        self.campus = baker.make(
            Pin,
            profile=self.profile,
            location=campus_location,
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
        )

    def building_child(self, index: int, *, outline: bool = True, parent: Pin | None = None) -> Pin:
        lng, lat = -73.930 + index * 0.0006, 41.7325
        place = make_place(PlaceKind.BUILDING, _square(lng, lat, 0.0001) if outline else None, parent=self.parcel)
        location = baker.make(Location, latitude=f"{lat:.6f}", longitude=f"{lng:.6f}", place=place)
        return baker.make(
            Pin, profile=self.profile, location=location, parent_pin=parent or self.campus, pin_type=PinType.BUILDING
        )

    def outlines(self, children: str | None) -> list[int]:
        url = reverse("boundary.pin", args=[self.campus.slug])
        response = self.client.get(url if children is None else f"{url}?children={children}")
        self.assertEqual(response.status_code, 200)
        return sorted(entry["pin_id"] for entry in json.loads(response.content)["detail_buildings"])


class BuildingChildOutlineTests(_Campus):
    def test_every_building_child_with_an_outline_is_drawn(self) -> None:
        children = [self.building_child(index) for index in range(3)]

        self.assertEqual(self.outlines("1"), sorted(child.pk for child in children))

    def test_none_while_child_details_are_off(self) -> None:
        self.building_child(0)
        drawn = self.building_child(1, outline=False)
        baker.make(
            Boundary,
            pin=drawn,
            profile=self.profile,
            location=drawn.location,
            boundary_type=BoundaryType.BUILDING,
            polygon=_square(-73.9294, 41.7325, 0.0001),
        )

        self.assertEqual(self.outlines("0"), [])

    def test_a_drawn_outline_counts_as_the_childs_own(self) -> None:
        drawn = self.building_child(0, outline=False)
        baker.make(
            Boundary,
            pin=drawn,
            profile=self.profile,
            location=drawn.location,
            boundary_type=BoundaryType.BUILDING,
            polygon=_square(-73.930, 41.7325, 0.0001),
        )

        self.assertEqual(self.outlines("1"), [drawn.pk])

    def test_a_building_with_no_outline_draws_nothing(self) -> None:
        self.building_child(0, outline=False)

        self.assertEqual(self.outlines("1"), [])

    def test_grandchildren_are_drawn_too(self) -> None:
        child = self.building_child(0)
        grandchild = self.building_child(1, parent=child)

        self.assertEqual(self.outlines("1"), sorted([child.pk, grandchild.pk]))

    def test_a_child_inside_its_parents_building_does_not_redraw_the_parents(self) -> None:
        building = self.building_child(0)
        inside = baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(Location, latitude="41.732520", longitude="-73.930020"),
            parent_pin=building,
        )

        self.assertNotIn(inside.pk, self.outlines("1"))

    def test_two_pins_in_one_building_draw_it_once(self) -> None:
        first = self.building_child(0)
        baker.make(
            Pin, profile=self.profile, location=first.location, parent_pin=self.campus, pin_type=PinType.BUILDING
        )

        self.assertEqual(self.outlines("1"), [first.pk])

    def test_one_outline_drawn_from_another_vertex_is_drawn_once(self) -> None:
        first = self.building_child(0, outline=False)
        second = self.building_child(1, outline=False)
        lng, lat, d = -73.9297, 41.7325, 0.0001
        rotated = ((lng + d, lat + d), (lng - d, lat + d), (lng - d, lat - d), (lng + d, lat - d), (lng + d, lat + d))
        baker.make(
            Boundary,
            pin=first,
            profile=self.profile,
            location=first.location,
            boundary_type=BoundaryType.BUILDING,
            polygon=_square(lng, lat, d),
        )
        baker.make(
            Boundary,
            pin=second,
            profile=self.profile,
            location=second.location,
            boundary_type=BoundaryType.BUILDING,
            polygon=MultiPolygon(Polygon(rotated, srid=4326), srid=4326),
        )

        self.assertEqual(self.outlines("1"), [first.pk])

    def test_without_the_parameter_the_children_are_drawn(self) -> None:
        """Callers that predate the toggle keep the old answer."""
        child = self.building_child(0)

        self.assertEqual(self.outlines(None), [child.pk])

    def test_the_query_count_does_not_grow_with_the_children(self) -> None:
        for index in range(2):
            self.building_child(index)
        url = reverse("boundary.pin", args=[self.campus.slug]) + "?children=1"
        self.client.get(url)

        with CaptureQueriesContext(connection) as few:
            self.client.get(url)
        for index in range(2, 8):
            self.building_child(index)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url)

        self.assertEqual(len(many), len(few))


class WikiChildOutlineTests(_Campus):
    """A campus wiki's map draws its child wikis' building outlines while child details are on, as a pin's map does."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=self.campus.location, name="Campus")

    def child_wiki(self, index: int, *, outline: bool = True, parent: Wiki | None = None) -> Wiki:
        lng, lat = -73.930 + index * 0.0006, 41.7325
        place = make_place(PlaceKind.BUILDING, _square(lng, lat, 0.0001) if outline else None, parent=self.parcel)
        location = baker.make(Location, latitude=f"{lat:.6f}", longitude=f"{lng:.6f}", place=place)
        return baker.make(Wiki, location=location, name=f"Building {index}", parent_wiki=parent or self.wiki)

    def wiki_outlines(self, children: str | None) -> list[int]:
        url = reverse("location.wiki.boundary", args=[self.campus.location.slug])
        response = self.client.get(url if children is None else f"{url}?children={children}")
        self.assertEqual(response.status_code, 200)
        return sorted(entry["wiki_id"] for entry in json.loads(response.content)["detail_buildings"])

    def test_every_child_wiki_with_an_outline_is_drawn(self) -> None:
        children = [self.child_wiki(index) for index in range(3)]
        self.child_wiki(3, outline=False)

        self.assertEqual(self.wiki_outlines("1"), sorted(child.pk for child in children))

    def test_none_while_child_details_are_off(self) -> None:
        self.child_wiki(0)

        self.assertEqual(self.wiki_outlines("0"), [])

    def test_without_the_parameter_none_are_drawn(self) -> None:
        """A wiki's map never drew them before the toggle reached it."""
        self.child_wiki(0)

        self.assertEqual(self.wiki_outlines(None), [])

    def test_grandchild_wikis_are_drawn_too(self) -> None:
        child = self.child_wiki(0)
        grandchild = self.child_wiki(1, parent=child)

        self.assertEqual(self.wiki_outlines("1"), sorted([child.pk, grandchild.pk]))

    def test_a_child_wikis_drawn_outline_counts(self) -> None:
        # Off the parcel, so its location stands on no place whose own outline would decide instead.
        drawn = baker.make(
            Wiki,
            location=baker.make(Location, latitude="41.745000", longitude="-73.930000"),
            name="Drawn",
            parent_wiki=self.wiki,
        )
        baker.make(
            Boundary,
            wiki=drawn,
            pin=None,
            profile=None,
            location=drawn.location,
            boundary_type=BoundaryType.BUILDING,
            polygon=_square(-73.930, 41.745, 0.0001),
        )

        self.assertEqual(self.wiki_outlines("1"), [drawn.pk])

    def test_the_query_count_does_not_grow_with_the_children(self) -> None:
        for index in range(2):
            self.child_wiki(index)
        url = reverse("location.wiki.boundary", args=[self.campus.location.slug]) + "?children=1"
        self.client.get(url)

        with CaptureQueriesContext(connection) as few:
            self.client.get(url)
        for index in range(2, 8):
            self.child_wiki(index)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url)

        self.assertEqual(len(many), len(few))
