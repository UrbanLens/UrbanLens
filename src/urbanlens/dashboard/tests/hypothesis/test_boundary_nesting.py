"""A boundary a user draws on their own pin groups that user's pins, and only theirs."""

from __future__ import annotations

from typing import Any

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.pin_edit import delete_pin, reparent_pin
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


def _pin(profile, dlat: float = 0.0, dlng: float = 0.0, *, run: bool = True):
    """A root pin; its nesting pass runs at once unless *run* is False (the caller captures the callbacks)."""
    location: Any = baker.make("dashboard.Location", latitude=f"{_LAT + dlat:.6f}", longitude=f"{_LNG + dlng:.6f}")
    if not run:
        return baker.make("dashboard.Pin", profile=profile, location=location, parent_pin=None)
    with TestCase.captureOnCommitCallbacks(execute=True):
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
        self.profile: Any = baker.make("dashboard.Profile")
        self.owner = _pin(self.profile)

    def _settle(self, *pins) -> None:
        for pin in pins:
            pin.refresh_from_db()

    def test_a_pin_inside_the_boundary_nests(self) -> None:
        inside = _pin(self.profile, 0.001)
        outside = _pin(self.profile, 0.5)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        self._settle(inside, outside)
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        self.assertEqual(inside.nested_by_boundary_pin_id, self.owner.pk)
        self.assertIsNone(outside.parent_pin_id)

    def test_a_new_pin_inside_an_existing_boundary_nests(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        late = _pin(self.profile, 0.002)
        self._settle(late)
        self.assertEqual(late.parent_pin_id, self.owner.pk)

    def test_another_users_pin_is_untouched(self) -> None:
        other: Any = baker.make("dashboard.Profile")
        theirs = _pin(other, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        self._settle(theirs)
        self.assertIsNone(theirs.parent_pin_id)
        self.assertIsNone(theirs.nested_by_boundary_pin_id)

    def test_a_detached_pin_stays_a_root_after_a_boundary_edit(self) -> None:
        inside = _pin(self.profile, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        inside.refresh_from_db()
        reparent_pin(inside, None)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.02)
        self._settle(inside)
        self.assertIsNone(inside.parent_pin_id)
        self.assertTrue(inside.parenting_is_manual)

    def test_a_hand_parented_pin_is_not_taken(self) -> None:
        inside = _pin(self.profile, 0.001)
        elsewhere = _pin(self.profile, 0.5)
        reparent_pin(inside, elsewhere)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        self._settle(inside)
        self.assertEqual(inside.parent_pin_id, elsewhere.pk)

    def test_a_pin_that_predates_nesting_stays_a_root_inside_an_old_boundary(self) -> None:
        existing = _pin(self.profile, 0.001)
        Pin.objects.filter(pk=existing.pk).update(parenting_is_manual=True)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        self._settle(existing)
        self.assertIsNone(existing.parent_pin_id)

    def test_the_flag_defaults_to_open_for_new_pins(self) -> None:
        self.assertFalse(Pin._meta.get_field("parenting_is_manual").default)

    def test_shrinking_returns_only_pins_this_mechanism_nested(self) -> None:
        near = _pin(self.profile, 0.001)
        far = _pin(self.profile, 0.008)
        arranged = _pin(self.profile, 0.009)
        reparent_pin(arranged, near)
        with self.captureOnCommitCallbacks(execute=True):
            boundary = _draw(self.owner, 0.01)
        far.refresh_from_db()
        self.assertEqual(far.parent_pin_id, self.owner.pk)
        with self.captureOnCommitCallbacks(execute=True):
            boundary.polygon = _square(0.002)
            boundary.save()
        self._settle(far, near, arranged)
        self.assertIsNone(far.parent_pin_id)
        self.assertEqual(near.parent_pin_id, self.owner.pk)
        self.assertEqual(arranged.parent_pin_id, near.pk)

    def test_deleting_the_boundary_returns_pins_to_root(self) -> None:
        inside = _pin(self.profile, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            boundary = _draw(self.owner, 0.01)
        inside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        with self.captureOnCommitCallbacks(execute=True):
            boundary.delete()
        self._settle(inside)
        self.assertIsNone(inside.parent_pin_id)
        self.assertIsNone(inside.nested_by_boundary_pin_id)

    def test_overlapping_boundaries_pick_the_smallest(self) -> None:
        small_owner = _pin(self.profile, 0.002)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
            _draw(small_owner, 0.001)
        in_both = _pin(self.profile, 0.0021)
        in_big_only = _pin(self.profile, 0.006)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(small_owner, 0.00101)
        self._settle(small_owner, in_both, in_big_only)
        self.assertEqual(in_both.parent_pin_id, small_owner.pk)
        self.assertEqual(in_big_only.parent_pin_id, self.owner.pk)
        self.assertEqual(small_owner.parent_pin_id, self.owner.pk)

    def test_no_cycles(self) -> None:
        other = _pin(self.profile, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
            _draw(other, 0.01)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.011)
            _draw(other, 0.011)
        self._settle(self.owner, other)
        self.assertEqual(other.parent_pin_id, self.owner.pk)
        self.assertIsNone(self.owner.parent_pin_id)
        for pin in (self.owner, other):
            self.assertNotIn(pin.pk, [ancestor.pk for ancestor in pin.ancestor_chain()])

    def test_a_boundary_grants_no_access(self) -> None:
        other: Any = baker.make("dashboard.Profile")
        theirs = _pin(other, 0.001)
        before_ids = visible_wiki_location_ids(self.profile)
        self.assertFalse(location_visible_to(theirs.location, self.profile))
        inside = _pin(self.profile, 0.002)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        inside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        self.assertFalse(location_visible_to(theirs.location, self.profile))
        self.assertFalse(location_visible_to(self.owner.location, other))
        self.assertEqual(
            visible_wiki_location_ids(self.profile) - {inside.location_id}, before_ids - {inside.location_id}
        )

    def test_deleting_the_owner_in_keep_mode_returns_nested_children_to_open_roots(self) -> None:
        nested = _pin(self.profile, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        nested.refresh_from_db()
        by_hand = _pin(self.profile, 0.5)
        child = _pin(self.profile, 0.6)
        reparent_pin(child, self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            delete_pin(self.owner, children_mode="keep")
        self._settle(nested, child)
        self.assertIsNone(nested.parent_pin_id)
        self.assertIsNone(nested.nested_by_boundary_pin_id)
        self.assertFalse(nested.parenting_is_manual)
        self.assertIsNone(child.parent_pin_id)
        self.assertTrue(child.parenting_is_manual)
        self.assertTrue(Pin.objects.filter(pk=by_hand.pk).exists())

    def test_a_moved_nested_pin_leaves_when_it_moves_out(self) -> None:
        inside = _pin(self.profile, 0.001)
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        inside.refresh_from_db()
        self.assertEqual(inside.parent_pin_id, self.owner.pk)
        far: Any = baker.make("dashboard.Location", latitude=f"{_LAT + 0.5:.6f}", longitude=f"{_LNG:.6f}")
        with self.captureOnCommitCallbacks(execute=True):
            inside.location = far
            inside.save(update_fields=["location", "updated"])
        self._settle(inside)
        self.assertIsNone(inside.parent_pin_id)
        self.assertIsNone(inside.nested_by_boundary_pin_id)

    def test_a_refit_of_the_generated_outline_does_not_run_a_pass(self) -> None:
        row = Boundary.objects.create(
            pin=self.owner,
            boundary_type="property",
            profile=self.profile,
            location=self.owner.location,
            generated_polygon=_square(0.01),
            generated_from_children=True,
        )
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            row.generated_polygon = _square(0.02)
            row.save(update_fields=["generated_polygon", "updated"])
            row.save()
        self.assertEqual(callbacks, [])

    def test_a_profile_with_no_drawn_boundary_costs_one_query_for_a_big_import(self) -> None:
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            pins = [_pin(self.profile, index / 100000, run=False) for index in range(1, 201)]
        self.assertEqual(len(pins), 200)
        passes = [callback for callback in callbacks if type(callback).__name__ == "_Pass"]
        self.assertEqual(len(passes), 1)
        with CaptureQueriesContext(connection) as queries:
            passes[0]()
        self.assertLessEqual(len(queries), 2)

    def test_a_big_import_into_a_boundary_is_a_bounded_number_of_queries(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            _draw(self.owner, 0.01)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            pins = [_pin(self.profile, index / 100000, run=False) for index in range(1, 201)]
        passes = [callback for callback in callbacks if type(callback).__name__ == "_Pass"]
        self.assertEqual(len(passes), 1)
        with CaptureQueriesContext(connection) as queries:
            passes[0]()
        self.assertLessEqual(len(queries), 200 * 12 + 5)
        self.assertEqual(Pin.objects.filter(pk__in=[pin.pk for pin in pins], parent_pin=self.owner).count(), 200)
