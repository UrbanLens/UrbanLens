"""One root pin per property holds when the second coordinate lands on one of the property's buildings.

The check compared the exact place, so once a building had a footprint of its own a point on it resolved
onto the building place rather than the parcel, and a second root pin on the same property was accepted."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import PlaceKind, PlaceRelation
from urbanlens.dashboard.services.pins.pin_creation import DuplicatePropertyError, create_pin_for_profile
from urbanlens.dashboard.services.places import lineage

from .place_helpers import make_place


def _square(x: float, y: float, size: float) -> MultiPolygon:
    return MultiPolygon(Polygon(((x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y))), srid=4326)


class OneRootPinPerPropertyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.parcel = make_place(PlaceKind.PARCEL, _square(-73.930, 41.730, 0.01), name="campus")
        self.building = make_place(PlaceKind.BUILDING, _square(-73.925, 41.734, 0.001), name="north east wing")
        lineage.set_parent(self.building, self.parcel, PlaceRelation.PART_OF)
        self.profile = baker.make(User).profile
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])

    def _pin(self, latitude: float, longitude: float):
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            return create_pin_for_profile(self.profile, name="probe", latitude=latitude, longitude=longitude).pin

    def test_a_point_on_a_building_is_the_property_already_pinned(self) -> None:
        first = self._pin(41.731, -73.929)
        self.assertEqual(first.location.place_id, self.parcel.pk)

        with self.assertRaises(DuplicatePropertyError):
            self._pin(41.7345, -73.9245)

    def test_the_parcel_is_the_property_already_pinned_from_a_building(self) -> None:
        first = self._pin(41.7345, -73.9245)
        self.assertEqual(first.location.place_id, self.building.pk)

        with self.assertRaises(DuplicatePropertyError):
            self._pin(41.731, -73.929)
