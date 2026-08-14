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
        child = Pin.objects.create(profile=self.profile, location=self._child_location(42.6527, -73.7563), parent_pin=self.parent)

        self.assertIsNotNone(child.pk)

    def test_deleting_a_child_pin_outside_a_transaction(self) -> None:
        child = Pin.objects.create(profile=self.profile, location=self._child_location(42.6528, -73.7564), parent_pin=self.parent)

        child.delete()

        self.assertFalse(Pin.objects.filter(pk=child.pk).exists())
