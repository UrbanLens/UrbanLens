"""Tests for exact-coordinate Location resolution.

``Location`` stores coordinates as fixed-precision decimals but builds its
PostGIS ``point`` from the raw unrounded float, so two submissions that differ
only below the stored precision round to the *same* (latitude, longitude) while
their points sit centimetres apart. ``get_nearby_or_create(threshold_meters=0)``
could not see that: its zero-distance probe missed the existing row, the insert
then tripped the ``(latitude, longitude)`` unique constraint, and the retry ran
the same failing probe again and re-raised - a 500.

``get_exact_or_create`` matches on the stored coordinates instead, which is what
actually decides identity, so the miss-then-collide sequence can't happen.

Also covers the two callers that were reaching that path: a child wiki placed on
coordinates another wiki already occupies (which additionally tried to insert a
duplicate Location outright), and a pin move.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from hypothesis import HealthCheck, given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.queryset import quantize_coordinate
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pins.pin_edit import PinMoveError, move_pin_to_coordinates

_db_settings = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

from .place_helpers import official_geometry


class GetExactOrCreateTests(TestCase):
    """``Location.objects.get_exact_or_create`` keys on stored precision."""

    def test_sub_precision_difference_reuses_the_same_row(self) -> None:
        """The regression: these round to one stored coordinate pair, so they
        must resolve to one row rather than colliding on insert."""
        first, created_first = Location.objects.get_exact_or_create(42.00000014, -73.00000014)
        second, created_second = Location.objects.get_exact_or_create(42.00000006, -73.00000006)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)

    def test_distinct_coordinates_get_distinct_rows(self) -> None:
        first, _ = Location.objects.get_exact_or_create(42.0001, -73.0001)
        second, _ = Location.objects.get_exact_or_create(42.0002, -73.0002)

        self.assertNotEqual(first.pk, second.pk)

    def test_does_not_snap_to_a_nearby_location(self) -> None:
        """Unlike get_nearby_or_create's default, proximity is irrelevant here."""
        near = Location.objects.create(latitude=42.0, longitude=-73.0)
        resolved, created = Location.objects.get_exact_or_create(42.0001, -73.0001)

        self.assertTrue(created)
        self.assertNotEqual(resolved.pk, near.pk)

    def test_defaults_apply_only_on_creation(self) -> None:
        created, _ = Location.objects.get_exact_or_create(42.5, -73.5, defaults={"official_name": "First"})
        again, was_created = Location.objects.get_exact_or_create(42.5, -73.5, defaults={"official_name": "Second"})

        self.assertFalse(was_created)
        self.assertEqual(again.pk, created.pk)
        again.refresh_from_db()
        self.assertEqual(again.official_name, "First")

    @given(
        lat=st.floats(min_value=-80.0, max_value=80.0, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
        nudge=st.floats(min_value=1e-9, max_value=9e-8, allow_nan=False, allow_infinity=False),
    )
    @_db_settings
    def test_resolution_agrees_exactly_with_stored_coordinates(self, lat: float, lon: float, nudge: float) -> None:
        """The real invariant: two submissions share a row precisely when they
        round to the same stored pair.

        Not "any tiny nudge lands on the same row" - a coordinate sitting on a
        rounding boundary (hypothesis finds e.g. -1.3203125) legitimately
        rounds to a different stored value under the smallest possible nudge.
        What must hold is that row identity tracks the stored coordinates,
        because those are what the unique constraint is on.
        """
        nudged_lat, nudged_lon = lat + nudge, lon + nudge
        same_point = quantize_coordinate(lat, "latitude") == quantize_coordinate(
            nudged_lat, "latitude"
        ) and quantize_coordinate(lon, "longitude") == quantize_coordinate(nudged_lon, "longitude")

        first, _ = Location.objects.get_exact_or_create(lat, lon)
        second, created = Location.objects.get_exact_or_create(nudged_lat, nudged_lon)

        if same_point:
            self.assertFalse(created)
            self.assertEqual(first.pk, second.pk)
        else:
            self.assertNotEqual(first.pk, second.pk)


class ChildWikiCoordinateCollisionTests(TestCase):
    """Placing a child wiki where a wiki already sits is refused, not a 500.

    ``Wiki.location`` is one-to-one, so the old code tried to sidestep the
    collision by inserting a *second* Location at the same coordinates - which
    the (latitude, longitude) unique constraint forbids outright.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        official_geometry(self.location, _square(-74.0, 40.0, 0.01))
        self.wiki = baker.make(Wiki, location=self.location, name="Old Asylum")
        baker.make(Pin, profile=self.profile, location=self.location)

    def _post(self, latitude: float, longitude: float, name: str = "Gatehouse"):
        return self.client.post(
            reverse("location.wiki.detail_pins.panel", kwargs={"location_slug": self.location.slug}),
            data=json.dumps({"name": name, "latitude": latitude, "longitude": longitude}),
            content_type="application/json",
        )

    def test_child_wiki_on_the_parent_wikis_own_point_is_refused(self) -> None:
        response = self._post(40.0, -74.0)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Wiki.objects.filter(parent_wiki=self.wiki).count(), 0)

    def test_child_wiki_on_a_sibling_child_wikis_point_is_refused(self) -> None:
        self.assertEqual(self._post(40.001, -74.001, name="First").status_code, 200)

        response = self._post(40.001, -74.001, name="Second")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Wiki.objects.filter(parent_wiki=self.wiki).count(), 1)

    def test_child_wiki_at_a_free_point_still_works(self) -> None:
        response = self._post(40.002, -74.002)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Wiki.objects.filter(parent_wiki=self.wiki).count(), 1)

    def test_a_location_without_a_wiki_is_reused_rather_than_duplicated(self) -> None:
        """A bare Location at those coordinates is free to take a wiki."""
        bare = Location.objects.create(latitude=40.003, longitude=-74.003)

        response = self._post(40.003, -74.003)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Wiki.objects.get(parent_wiki=self.wiki).location_id, bare.pk)


class MovePinToCoordinatesTests(TestCase):
    """``move_pin_to_coordinates`` resolves exactly and reports collisions."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make(
            Pin, profile=self.profile, location=Location.objects.create(latitude=10.0, longitude=20.0)
        )

    def test_sub_precision_move_onto_an_existing_location_does_not_error(self) -> None:
        existing = Location.objects.create(latitude=30.00000014, longitude=40.00000014)

        move_pin_to_coordinates(self.pin, 30.00000006, 40.00000006)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, existing.pk)

    def test_move_reports_a_collision_with_the_owners_other_root_pin(self) -> None:
        """Two root pins can't share a Location - that's a DB constraint, and
        it must surface as a typed error rather than an IntegrityError 500."""
        other = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=50.0, longitude=60.0))

        with self.assertRaises(PinMoveError):
            move_pin_to_coordinates(self.pin, 50.0, 60.0)

        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.location_id, other.location_id)

    def test_another_profiles_pin_never_blocks_a_move(self) -> None:
        stranger = baker.make(User).profile
        shared = Location.objects.create(latitude=55.0, longitude=65.0)
        baker.make(Pin, profile=stranger, location=shared)

        move_pin_to_coordinates(self.pin, 55.0, 65.0)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, shared.pk)

    def test_a_child_pin_may_move_onto_a_location_holding_a_root_pin(self) -> None:
        """The uniqueness rule is root-pins-only; child pins keep their freedom
        to share a parcel, so the collision check must not over-reach."""
        root = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=70.0, longitude=80.0))
        child = baker.make(
            Pin, profile=self.profile, parent_pin=root, location=Location.objects.create(latitude=71.0, longitude=81.0)
        )

        move_pin_to_coordinates(child, 70.0, 80.0)

        child.refresh_from_db()
        self.assertEqual(child.location_id, root.location_id)


def _square(lng: float, lat: float, delta: float):
    from django.contrib.gis.geos import MultiPolygon, Polygon

    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)
