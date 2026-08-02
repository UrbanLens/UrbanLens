"""Tests for the child-pin exact-coordinate overlap rule.

Two pins belonging to one profile must never sit at precisely the same
coordinates. Root pins have always been protected by the
``db_pin_unique_location_per_profile`` constraint, but child (detail) pins are
deliberately exempt from it - they need to be able to share a *parcel* with
their parent and siblings. That exemption was total, so nothing stopped two
child pins (or a child pin and its own parent) from stacking exactly on top of
each other, which is unrenderable: the markers overlap perfectly, so there is
no way to click the one underneath or to tell the two apart on the map.

The rule is exact-coordinate only. Child pins placed *near* each other stay
legal - marking a door, a window, and a sign on one small building is the
feature child pins exist for.

Also covers ``resolve_child_pin_location``'s coordinate quantization: Location
identity is (latitude, longitude) rounded to the field's 6 decimal places, so
resolution matches on those rounded values rather than on a zero-distance
PostGIS comparison against a point built from the raw unrounded float.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.pin_creation import PinCreationError, create_pin_for_profile, resolve_child_pin_location

# DB-backed @given tests never touch self.client - only ORM/service calls - per
# this repo's documented rule that hypothesis's per-example DB flush and the
# Django test client don't mix.
_db_settings = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


class ChildPinExactOverlapViewTests(TestCase):
    """POST /map/pin/<slug>/detail-pins/ rejects a child pin stacked on another pin."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=42.0, longitude=-73.0))
        self.root.slug = self.root.ensure_slug()

    def _create(self, name: str, latitude: float, longitude: float):
        return self.client.post(
            reverse("pin.detail_pins", kwargs={"pin_slug": self.root.slug}),
            data=json.dumps({"name": name, "latitude": latitude, "longitude": longitude}),
            content_type="application/json",
        )

    def _move(self, detail_pin: Pin, latitude: float, longitude: float):
        return self.client.post(
            reverse("pin.detail_pin.edit", kwargs={"pin_slug": self.root.slug, "detail_pin_uuid": detail_pin.uuid}),
            data=json.dumps({"latitude": latitude, "longitude": longitude}),
            content_type="application/json",
        )

    def test_child_pin_at_a_siblings_exact_coordinates_is_rejected(self) -> None:
        self.assertEqual(self._create("First", 42.00010, -73.00010).status_code, 200)
        response = self._create("Second", 42.00010, -73.00010)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Pin.objects.filter(profile=self.profile, parent_pin=self.root).count(), 1)

    def test_child_pin_at_the_parents_exact_coordinates_is_rejected(self) -> None:
        """The parent occupies its own point; a child stacked on it is unclickable."""
        response = self._create("Stacked on parent", 42.0, -73.0)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Pin.objects.filter(profile=self.profile, parent_pin=self.root).exists())

    def test_nearby_child_pins_are_still_allowed(self) -> None:
        """~15m apart - the door/window/sign case child pins exist for."""
        first = self._create("Door", 42.00010, -73.00010)
        second = self._create("Window", 42.00020, -73.00020)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_pin = Pin.objects.get(uuid=first.json()["uuid"])
        second_pin = Pin.objects.get(uuid=second.json()["uuid"])
        self.assertNotEqual(first_pin.location_id, second_pin.location_id)

    def test_another_profiles_pin_at_the_same_point_does_not_block(self) -> None:
        """The rule is per-profile: Locations are shared, pins are personal."""
        other = baker.make(User).profile
        shared = Location.objects.create(latitude=42.00030, longitude=-73.00030)
        baker.make(Pin, profile=other, location=shared)

        response = self._create("Mine", 42.00030, -73.00030)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Pin.objects.get(uuid=response.json()["uuid"]).location_id, shared.pk)

    def test_moving_a_child_pin_onto_another_pins_exact_point_is_rejected(self) -> None:
        self._create("First", 42.00010, -73.00010)
        second = Pin.objects.get(uuid=self._create("Second", 42.00020, -73.00020).json()["uuid"])
        location_before = second.location_id

        response = self._move(second, 42.00010, -73.00010)

        self.assertEqual(response.status_code, 400)
        second.refresh_from_db()
        self.assertEqual(second.location_id, location_before)

    def test_moving_a_child_pin_to_its_own_current_point_is_allowed(self) -> None:
        """A no-op move (e.g. a drag that snaps back) must not trip the rule."""
        child = Pin.objects.get(uuid=self._create("Door", 42.00010, -73.00010).json()["uuid"])

        response = self._move(child, 42.00010, -73.00010)

        self.assertEqual(response.status_code, 200)

    def test_moving_a_child_pin_to_a_free_point_still_works(self) -> None:
        child = Pin.objects.get(uuid=self._create("Door", 42.00010, -73.00010).json()["uuid"])
        location_before = child.location_id

        response = self._move(child, 42.00050, -73.00050)

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertNotEqual(child.location_id, location_before)


class ChildPinExactOverlapServiceTests(TestCase):
    """``create_pin_for_profile(parent_id=...)`` enforces the same rule as the map UI."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.root = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=41.0, longitude=-75.0))

    def test_child_stacked_on_its_parent_is_rejected(self) -> None:
        with self.assertRaises(PinCreationError):
            create_pin_for_profile(self.profile, name="Stacked", latitude=41.0, longitude=-75.0, parent_id=self.root.uuid)

    def test_child_near_its_parent_is_accepted(self) -> None:
        result = create_pin_for_profile(self.profile, name="Entrance", latitude=41.00010, longitude=-75.00010, parent_id=self.root.uuid)

        self.assertTrue(result.created)
        self.assertEqual(result.pin.parent_pin_id, self.root.pk)
        self.assertNotEqual(result.pin.location_id, self.root.location_id)

    def test_resolver_rejects_a_point_the_profile_already_pinned(self) -> None:
        with self.assertRaises(PinCreationError):
            resolve_child_pin_location(self.profile, 41.0, -75.0)

    def test_resolver_ignores_the_pin_being_moved(self) -> None:
        """Re-resolving a pin's own current point is what a no-op move does."""
        location = resolve_child_pin_location(self.profile, 41.0, -75.0, exclude_pin=self.root)
        self.assertEqual(location.pk, self.root.location_id)

    def test_resolver_reuses_an_existing_location_row(self) -> None:
        """Locations are shared; only the *pin* is barred from stacking."""
        other = baker.make(User).profile
        existing = Location.objects.create(latitude=41.00020, longitude=-75.00020)
        baker.make(Pin, profile=other, location=existing)

        self.assertEqual(resolve_child_pin_location(self.profile, 41.00020, -75.00020).pk, existing.pk)

    def test_resolver_quantizes_to_the_stored_precision(self) -> None:
        """Location identity is (lat, lon) at the field's 6dp, so two inputs that
        round to the same stored coordinate must resolve to the same row rather
        than racing the (latitude, longitude) unique constraint."""
        first = resolve_child_pin_location(self.profile, 41.00030004, -75.00030004)
        second = resolve_child_pin_location(self.profile, 41.00030001, -75.00030001)

        self.assertEqual(first.pk, second.pk)


class ChildPinLocationResolutionPropertyTests(TestCase):
    """Property-based generalization of the rule above.

    Calls ``resolve_child_pin_location`` directly rather than through
    self.client, per this repo's documented @given + self.client
    incompatibility.
    """

    @given(
        lat=st.floats(min_value=-80.0, max_value=80.0, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
        lat_offset=st.floats(min_value=0.0001, max_value=0.001, allow_nan=False, allow_infinity=False),
        lon_offset=st.floats(min_value=0.0001, max_value=0.001, allow_nan=False, allow_infinity=False),
    )
    @_db_settings
    def test_distinct_points_resolve_to_distinct_locations(self, lat: float, lon: float, lat_offset: float, lon_offset: float) -> None:
        """Nearby-but-distinct child pins keep their own coordinates - no proximity snap."""
        profile = baker.make(User).profile

        first = resolve_child_pin_location(profile, lat, lon)
        second = resolve_child_pin_location(profile, lat + lat_offset, lon + lon_offset)

        self.assertNotEqual(first.pk, second.pk)

    @given(
        lat=st.floats(min_value=-80.0, max_value=80.0, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
    )
    @_db_settings
    def test_a_point_the_profile_already_pinned_is_always_rejected(self, lat: float, lon: float) -> None:
        profile = baker.make(User).profile
        location = resolve_child_pin_location(profile, lat, lon)
        baker.make(Pin, profile=profile, location=location)

        with self.assertRaises(PinCreationError):
            resolve_child_pin_location(profile, lat, lon)
