"""Detaching a pin from its shared Location is not an action the app offers.

Two things are asserted here, and the second is what keeps the first honest.

``pin.link`` accepts GET only - it renders the relink picker. A POST to it once
meant "detach", and detaching cannot be satisfied: ``Pin.effective_latitude``
*is* ``location.latitude``, and a database trigger
(``dashboard_locations_freeze_identity``) makes a Location's coordinates
immutable, so a pin's point is always exactly its location's. Giving a pin its
own Location at the same point therefore collides with Location's uniqueness on
(latitude, longitude), and the only way around it is to silently move somebody's
pin. A pin that should not share a place's record wants a *different* place,
which is what relinking already does.

``LocationIdentityTests`` asserts the two properties that make that true, since
both are the kind of thing a future change could quietly relax - and relaxing
either would reopen the question without anyone noticing.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class PinDetachLocationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(
            Location, latitude=41.7, longitude=-73.9, official_name="Hudson River State Hospital"
        )
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None, slug="hrsh")

    def _detach(self):
        return self.client.post(reverse("pin.link", kwargs={"pin_slug": self.pin.slug}))

    def test_posting_to_the_picker_route_is_not_allowed(self) -> None:
        response = self._detach()

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")

    def test_the_picker_route_still_answers_a_get(self) -> None:
        response = self.client.get(reverse("pin.link", kwargs={"pin_slug": self.pin.slug}))

        self.assertEqual(response.status_code, 200)

    def test_the_pin_keeps_its_location(self) -> None:
        original = self.pin.location_id

        self._detach()

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, original)

    def test_no_orphan_location_is_left_behind(self) -> None:
        before = Location.objects.count()

        self._detach()

        self.assertEqual(Location.objects.count(), before)

    # Relinking - the action detach was reaching for - has its own coverage
    # (test_pin_relink*.py). Not re-tested here: its target must pass the
    # access check that stops relinking being a way to *earn* a community wiki,
    # which needs visibility fixtures irrelevant to this route.


class LocationIdentityTests(TestCase):
    """Why detach cannot be satisfied - asserted, not assumed."""

    def test_a_pins_point_is_its_locations_point(self) -> None:
        location = baker.make(Location, latitude=41.73332, longitude=-73.92794)
        pin = baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)

        self.assertEqual(pin.effective_latitude, float(location.latitude))
        self.assertEqual(pin.effective_longitude, float(location.longitude))

    def test_a_locations_coordinates_cannot_be_moved(self) -> None:
        from django.db import IntegrityError, transaction

        location = baker.make(Location, latitude=41.5, longitude=-73.5)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Location.objects.filter(pk=location.pk).update(latitude=41.6)
