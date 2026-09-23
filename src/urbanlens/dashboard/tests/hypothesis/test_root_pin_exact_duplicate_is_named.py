"""A second root pin at the exact coordinates of one you already have is refused as exactly that.

It was reported as "you already have a pin on this property", which on a coordinate that resolves
onto no property at all sends the reader looking for a property that does not exist."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.pins.pin_creation import DuplicateCoordinatesError, create_pin_for_profile

_LAT, _LNG = 41.719043, -73.932924


class ExactDuplicateRootPinTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        self.first = create_pin_for_profile(self.profile, name="probe", latitude=_LAT, longitude=_LNG).pin

    def test_the_second_is_refused_as_a_duplicate_of_these_coordinates(self) -> None:
        self.assertIsNone(self.first.location.place_id, "the premise is a coordinate on no known property")

        with (
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
            self.assertRaises(DuplicateCoordinatesError),
        ):
            create_pin_for_profile(self.profile, name="probe again", latitude=_LAT, longitude=_LNG)
