"""A boundary a user draws on their own pin groups that user's pins, and only theirs."""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.services.pins.boundary_nesting import nest_pins_for_profile
from urbanlens.dashboard.services.pins.pin_edit import reparent_pin
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, visible_wiki_location_ids

_LAT, _LNG = 40.0, -74.0


def _square(half: float, lat: float = _LAT, lng: float = _LNG) -> MultiPolygon:
    ring = (
        (lng - half, lat - half),
        (lng + half, lat - half),
        (lng + half, lat + half),
        (lng - half, lat + half),
        (lng - half, lat - half),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def _pin(profile, dlat: float = 0.0, dlng: float = 0.0):
    location = baker.make("dashboard.Location", latitude=f"{_LAT + dlat:.6f}", longitude=f"{_LNG + dlng:.6f}")
    return baker.make("dashboard.Pin", profile=profile, location=location, parent_pin=None)


def _draw(pin, half: float) -> Boundary:
    return Boundary.objects.update_or_create(
        pin=pin,
        boundary_type="property",
        defaults={
            "profile": pin.profile,
            "location": pin.location,
            "polygon": _square(half, float(pin.location.latitude), float(pin.location.longitude)),
        },
    )[0]


class BoundaryNestingTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make("dashboard.Profile")
        self.owner = _pin(self.profile)

    def test_a_pin_inside_the_boundary_nests(self) -> None:
        inside = _pin(self.profile, 0.001)
        outside = _pin(self.profile, 0.5)
        _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        inside.refresh_from_db()
        outside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        self.assertEqual(inside.nested_by_boundary_pin_id, self.owner.pk)
        self.assertIsNone(outside.parent_pin_id)

    def test_another_users_pin_is_untouched(self) -> None:
        other = baker.make("dashboard.Profile")
        theirs = _pin(other, 0.001)
        _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.parent_pin_id)
        self.assertIsNone(theirs.nested_by_boundary_pin_id)
        self.assertEqual(nest_pins_for_profile(other.pk), 0)

    def test_a_detached_pin_stays_a_root_after_a_boundary_edit(self) -> None:
        inside = _pin(self.profile, 0.001)
        _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        inside.refresh_from_db()
        reparent_pin(inside, None)
        _draw(self.owner, 0.02)
        nest_pins_for_profile(self.profile.pk)
        inside.refresh_from_db()
        self.assertIsNone(inside.parent_pin_id)
        self.assertTrue(inside.parenting_is_manual)

    def test_a_hand_parented_pin_is_not_taken(self) -> None:
        inside = _pin(self.profile, 0.001)
        elsewhere = _pin(self.profile, 0.5)
        reparent_pin(inside, elsewhere)
        _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        inside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, elsewhere.pk)

    def test_shrinking_returns_only_pins_this_mechanism_nested(self) -> None:
        near = _pin(self.profile, 0.001)
        far = _pin(self.profile, 0.008)
        arranged = _pin(self.profile, 0.009)
        reparent_pin(arranged, near)
        boundary = _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        far.refresh_from_db()
        self.assertEqual(far.parent_pin_id, self.owner.pk)
        boundary.polygon = _square(0.002)
        boundary.save()
        nest_pins_for_profile(self.profile.pk)
        far.refresh_from_db()
        near.refresh_from_db()
        arranged.refresh_from_db()
        self.assertIsNone(far.parent_pin_id)
        self.assertEqual(near.parent_pin_id, self.owner.pk)
        self.assertEqual(arranged.parent_pin_id, near.pk)

    def test_clearing_the_boundary_returns_pins_to_root(self) -> None:
        inside = _pin(self.profile, 0.001)
        boundary = _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        boundary.delete()
        nest_pins_for_profile(self.profile.pk)
        inside.refresh_from_db()
        self.assertIsNone(inside.parent_pin_id)

    def test_overlapping_boundaries_pick_the_smallest(self) -> None:
        small_owner = _pin(self.profile, 0.002)
        _draw(self.owner, 0.01)
        _draw(small_owner, 0.001)
        in_both = _pin(self.profile, 0.0021)
        in_big_only = _pin(self.profile, 0.006)
        nest_pins_for_profile(self.profile.pk)
        for pin in (small_owner, in_both, in_big_only):
            pin.refresh_from_db()
        self.assertEqual(in_both.parent_pin_id, small_owner.pk)
        self.assertEqual(in_big_only.parent_pin_id, self.owner.pk)
        self.assertEqual(small_owner.parent_pin_id, self.owner.pk)

    def test_no_cycles(self) -> None:
        other = _pin(self.profile, 0.001)
        _draw(self.owner, 0.01)
        _draw(other, 0.01)
        nest_pins_for_profile(self.profile.pk)
        nest_pins_for_profile(self.profile.pk)
        self.owner.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(self.owner.parent_pin_id == other.pk and other.parent_pin_id == self.owner.pk)
        for pin in (self.owner, other):
            self.assertNotIn(pin.pk, [ancestor.pk for ancestor in pin.ancestor_chain()])

    def test_a_boundary_grants_no_access(self) -> None:
        other = baker.make("dashboard.Profile")
        theirs = _pin(other, 0.001)
        before_ids = visible_wiki_location_ids(self.profile)
        self.assertFalse(location_visible_to(theirs.location, self.profile))
        inside = _pin(self.profile, 0.002)
        _draw(self.owner, 0.01)
        nest_pins_for_profile(self.profile.pk)
        self.assertFalse(location_visible_to(theirs.location, self.profile))
        self.assertFalse(location_visible_to(self.owner.location, other))
        self.assertEqual(
            visible_wiki_location_ids(self.profile) - {inside.location_id}, before_ids - {inside.location_id}
        )

    def test_saving_a_boundary_or_a_pin_requests_nesting(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            inside = _pin(self.profile, 0.001)
            _draw(self.owner, 0.01)
        inside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        with self.captureOnCommitCallbacks(execute=True):
            late = _pin(self.profile, 0.002)
        late.refresh_from_db()
        self.assertEqual(late.parent_pin_id, self.owner.pk)
