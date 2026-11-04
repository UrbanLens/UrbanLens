"""Duplicate root pins on one property nest under the oldest once their Locations resolve, and never merge."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.services.pins.pin_creation import DuplicatePropertyError, create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_edit import reparent_pin
from urbanlens.dashboard.services.pins.property_nesting import (
    deferring_property_nesting,
    nest_duplicate_roots,
    request_property_nesting,
)
from urbanlens.dashboard.services.places.resolution import resolve_location_place

from .place_helpers import make_place

_X, _Y, _SIZE = -73.930, 41.730, 0.01


def _square(x: float, y: float, size: float) -> MultiPolygon:
    return MultiPolygon(Polygon(((x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y))), srid=4326)


class PropertyNestingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.other = baker.make(User).profile
        self.parcel = None

    def _root(self, profile, index: int, *, age: int = 0) -> Pin:
        """A root pin on its own Location inside the future parcel; older for a larger ``age``."""
        location = Location.objects.get_exact_or_create(_Y + 0.001 + index * 0.0005, _X + 0.001)[0]
        pin = Pin.objects.create(profile=profile, location=location, name=f"pin {index}")
        Pin.objects.filter(pk=pin.pk).update(created=timezone.now() - timedelta(days=age))
        return pin

    def _provision(self) -> list[Location]:
        self.parcel = make_place(PlaceKind.PARCEL, _square(_X, _Y, _SIZE), name="parcel")
        locations = list(Location.objects.filter(place__isnull=True))
        with self.captureOnCommitCallbacks(execute=True):
            for location in locations:
                resolve_location_place(location)
        return locations

    def _parents(self) -> dict[str, int | None]:
        return dict(Pin.objects.values_list("name", "parent_pin_id"))

    def test_imported_roots_nest_under_the_oldest(self) -> None:
        old = self._root(self.profile, 1, age=5)
        newer = self._root(self.profile, 2, age=3)
        newest = self._root(self.profile, 3, age=1)
        self._provision()
        for pin in (old, newer, newest):
            pin.refresh_from_db()
        self.assertIsNone(old.parent_pin_id)
        self.assertEqual(newer.parent_pin_id, old.pk)
        self.assertEqual(newest.parent_pin_id, old.pk)
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 3)

    def test_a_manual_root_is_never_renested(self) -> None:
        old = self._root(self.profile, 1, age=5)
        detached = self._root(self.profile, 2, age=3)
        Pin.objects.filter(pk=detached.pk).update(parenting_is_manual=True)
        self._provision()
        detached.refresh_from_db()
        self.assertIsNone(detached.parent_pin_id)
        old.refresh_from_db()
        self.assertIsNone(old.parent_pin_id)

    def test_detach_marks_a_pin_manual_so_it_stays_a_root(self) -> None:
        old = self._root(self.profile, 1, age=5)
        newer = self._root(self.profile, 2, age=3)
        self._provision()
        newer.refresh_from_db()
        self.assertEqual(newer.parent_pin_id, old.pk)
        reparent_pin(newer, None)
        nest_duplicate_roots(profile_id=self.profile.pk)
        newer.refresh_from_db()
        self.assertIsNone(newer.parent_pin_id)
        self.assertTrue(newer.parenting_is_manual)

    def test_a_manual_older_target_still_receives_newer_roots(self) -> None:
        old = self._root(self.profile, 1, age=5)
        Pin.objects.filter(pk=old.pk).update(parenting_is_manual=True)
        newer = self._root(self.profile, 2, age=3)
        self._provision()
        newer.refresh_from_db()
        self.assertEqual(newer.parent_pin_id, old.pk)

    def test_another_profiles_pin_is_untouched(self) -> None:
        mine = self._root(self.profile, 1, age=5)
        theirs = self._root(self.other, 1, age=9)
        # Same coordinates would share a Location; two profiles' roots on it are legal.
        self._provision()
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertIsNone(theirs.parent_pin_id)
        self.assertIsNone(mine.parent_pin_id)

    def test_running_twice_changes_nothing(self) -> None:
        self._root(self.profile, 1, age=5)
        self._root(self.profile, 2, age=3)
        self._provision()
        before = self._parents()
        self.assertEqual(nest_duplicate_roots(profile_id=self.profile.pk), [])
        self.assertEqual(self._parents(), before)

    def test_a_batch_of_200_runs_in_bounded_queries(self) -> None:
        for index in range(200):
            self._root(self.profile, index, age=300 - index)
        self.parcel = make_place(PlaceKind.PARCEL, _square(_X, _Y, _SIZE), name="parcel")
        location_ids = list(Location.objects.filter(place__isnull=True).values_list("pk", flat=True))
        Location.objects.filter(pk__in=location_ids).update(place=self.parcel)
        with (
            self.captureOnCommitCallbacks(execute=True),
            CaptureQueriesContext(connection) as queries,
            deferring_property_nesting() as nested,
        ):
            for location_id in location_ids:
                request_property_nesting(location_id)
        self.assertEqual(len(nested), 199)
        self.assertEqual(Pin.objects.filter(profile=self.profile, parent_pin__isnull=True).count(), 1)
        self.assertLess(len(queries), 40)

    def test_interactive_create_still_errors_with_the_building_hint(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        make_place(PlaceKind.PARCEL, _square(_X, _Y, _SIZE), name="parcel")
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            first = create_pin_for_profile(self.profile, name="a", latitude=_Y + 0.002, longitude=_X + 0.002).pin
            with self.assertRaises(DuplicatePropertyError) as caught:
                create_pin_for_profile(self.profile, name="b", latitude=_Y + 0.004, longitude=_X + 0.004)
        self.assertEqual(caught.exception.existing_pin_uuid, str(first.uuid))
