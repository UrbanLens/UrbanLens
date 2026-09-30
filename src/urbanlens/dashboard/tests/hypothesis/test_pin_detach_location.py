"""Detaching a pin from its shared Location is not an action the app offers."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse
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
        return self.client.post(f"/dashboard/map/pin/{self.pin.slug}/link/")

    def test_the_switch_wiki_picker_is_gone(self) -> None:
        """Jess, 2026-09-30: removed. Relinking from the wiki page's list stays (``pin.link.to``)."""
        with self.assertRaises(NoReverseMatch):
            reverse("pin.link", kwargs={"pin_slug": self.pin.slug})
        self.assertEqual(self.client.get(f"/dashboard/map/pin/{self.pin.slug}/link/").status_code, 404)
        self.assertFalse(
            (
                Path(__file__).resolve().parents[2] / "templates/dashboard/partials/pins/pin_location_picker.html"
            ).exists()
        )

    def test_relinking_to_a_named_location_is_still_a_post(self) -> None:
        url = reverse("pin.link.to", kwargs={"pin_slug": self.pin.slug, "location_slug": self.location.slug})
        self.assertEqual(self.client.get(url).status_code, 405)

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
