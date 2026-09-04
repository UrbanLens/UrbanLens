"""Child-pin saves must keep working outside an ambient transaction.

`refit_child_pin_boundary` runs from Pin's post_save/post_delete signals and takes a
`select_for_update` lock, which raises `TransactionManagementError` outside a transaction.
It is safe because the function carries `@transaction.atomic` - and that decorator is the
only thing making it safe: `ATOMIC_REQUESTS` is unset (Django defaults it to False), and
`Pin._meta.parents` is empty, so `Model.save_base` uses `mark_for_rollback_on_error` rather
than opening a transaction of its own.

These tests pin that guarantee. Remove the decorator and they fail.

They use **TransactionTestCase** deliberately: the ordinary `TestCase` wraps every test in a
transaction, which would satisfy `select_for_update` for free and make the guarantee
untestable - the rest of the suite genuinely cannot observe this.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.test import TransactionTestCase
from model_bakery import baker

from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class ChildPinOutsideTransactionTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        loc = baker.make(Location, latitude=42.6526, longitude=-73.7562, point=Point(-73.7562, 42.6526, srid=4326))
        self.parent = baker.make(Pin, profile=self.profile, location=loc)

    def _child_location(self, lat: float, lng: float) -> Location:
        return baker.make(Location, latitude=lat, longitude=lng, point=Point(lng, lat, srid=4326))

    def test_creating_a_child_pin_outside_a_transaction(self) -> None:
        """No explicit atomic() here, matching a view under ATOMIC_REQUESTS=False."""
        child = Pin.objects.create(
            profile=self.profile, location=self._child_location(42.6527, -73.7563), parent_pin=self.parent
        )

        self.assertIsNotNone(child.pk)
        # Not just "didn't raise": the refit must have actually run to completion
        # and fitted a stand-in, not merely survived by swallowing the error.
        self.assertTrue(
            Boundary.objects.filter(
                pin=self.parent, boundary_type=BoundaryType.PROPERTY, generated_from_children=True
            ).exists(),
            "the child-pin signal should have fitted a stand-in boundary for the parent",
        )

    def test_deleting_a_child_pin_outside_a_transaction(self) -> None:
        child = Pin.objects.create(
            profile=self.profile, location=self._child_location(42.6528, -73.7564), parent_pin=self.parent
        )
        self.assertTrue(
            Boundary.objects.filter(
                pin=self.parent, boundary_type=BoundaryType.PROPERTY, generated_from_children=True
            ).exists(),
            "the setup step should have fitted a stand-in boundary for the parent",
        )

        child.delete()

        self.assertFalse(Pin.objects.filter(pk=child.pk).exists())
        # The refit that runs from post_delete must also complete for real: the
        # last child is gone, so the stand-in it justified must be dropped too.
        self.assertFalse(
            Boundary.objects.filter(pin=self.parent, boundary_type=BoundaryType.PROPERTY).exists(),
            "the last child was removed, so the child-fitted stand-in should have been dropped",
        )

    def test_reparenting_a_child_pin_outside_a_transaction(self) -> None:
        """A hierarchy move refits the old and new parent as two separate atomic calls.

        Untested by the create/delete cases above: ``refit_child_boundaries_on_save``
        loops over up to two parent ids for a single save, each its own
        ``@transaction.atomic`` call, still with no ambient transaction wrapping
        the loop itself.
        """
        other_parent = baker.make(Pin, profile=self.profile, location=self._child_location(42.7000, -73.8000))
        child = Pin.objects.create(
            profile=self.profile, location=self._child_location(42.6528, -73.7564), parent_pin=self.parent
        )
        self.assertTrue(
            Boundary.objects.filter(
                pin=self.parent, boundary_type=BoundaryType.PROPERTY, generated_from_children=True
            ).exists()
        )

        child.parent_pin = other_parent
        child.save()

        self.assertEqual(Pin.objects.get(pk=child.pk).parent_pin_id, other_parent.pk)
        self.assertFalse(
            Boundary.objects.filter(pin=self.parent, boundary_type=BoundaryType.PROPERTY).exists(),
            "the old parent lost its only child and should have had its stand-in dropped",
        )
        self.assertTrue(
            Boundary.objects.filter(
                pin=other_parent, boundary_type=BoundaryType.PROPERTY, generated_from_children=True
            ).exists(),
            "the new parent gained a child and should have a freshly fitted stand-in",
        )
