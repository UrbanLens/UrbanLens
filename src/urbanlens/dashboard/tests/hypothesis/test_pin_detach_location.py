"""Detaching a pin from its shared Location must not 500.

Filed 2026-08-13 and reproduced: the detach branch called
``Location.objects.create(latitude=pin.effective_latitude, ...)``, but a pin's
coordinates *are* its current location's, and Location is unique on
(latitude, longitude) - so the row it tried to create always already existed.
Every attempt was an IntegrityError.

The filing left the product decision open. What it resolves to, given the
model: a pin attaches to a *nearby* Location, not only an exact one, so
detaching is meaningful exactly when the pin sits somewhere the shared record
does not. Then it gets its own Location there. When the shared record is at
the pin's exact point there is no second Location to move to, and saying so is
better than a 500 - or than silently doing nothing and reporting success.
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

    def _pin_at(self, *, pin_lat: float, location_lat: float) -> Pin:
        location = baker.make(Location, latitude=location_lat, longitude=-73.9, official_name="Hudson River State Hospital")
        # Slugs route through [-a-zA-Z0-9_]+, so no decimal point in it.
        self._seq = getattr(self, "_seq", 0) + 1
        pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug=f"pin-{self._seq}")
        if pin_lat != location_lat:
            # The pin's own point, distinct from the shared record's - which is
            # what a nearby-match attachment produces.
            Location.objects.filter(pk=location.pk).update(latitude=location_lat)
            pin.refresh_from_db()
        return pin

    def _detach(self, pin: Pin):
        return self.client.post(reverse("pin.link", kwargs={"pin_slug": pin.slug}), {})

    def test_detaching_a_pin_on_its_shared_point_explains_rather_than_500s(self) -> None:
        pin = self._pin_at(pin_lat=41.7, location_lat=41.7)

        response = self._detach(pin)

        self.assertEqual(response.status_code, 400, "this used to be an IntegrityError 500 on every attempt")
        self.assertIn(b"nothing to detach", response.content)

    def test_the_pin_keeps_its_location_when_detaching_is_refused(self) -> None:
        pin = self._pin_at(pin_lat=41.7, location_lat=41.7)
        original = pin.location_id

        self._detach(pin)

        pin.refresh_from_db()
        self.assertEqual(pin.location_id, original)

    def test_no_orphan_location_is_left_behind_by_a_refusal(self) -> None:
        pin = self._pin_at(pin_lat=41.7, location_lat=41.7)
        before = Location.objects.count()

        self._detach(pin)

        self.assertEqual(Location.objects.count(), before)
