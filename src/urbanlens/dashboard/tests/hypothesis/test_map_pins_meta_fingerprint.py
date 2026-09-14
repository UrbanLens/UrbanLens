"""Deleting a pin has to be visible to a client that is only polling."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state


class DeletingAPinChangesWhatTheClientPollsTests(TestCase):
    """The case a maximum cannot see."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        # Created in order, so the last one holds the maximum `updated`.
        self.pins = [
            baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=float(index), longitude=1.0))
            for index in range(3)
        ]

    def _meta(self) -> dict:
        """The poll response the map page reads.

        Returns:
            The decoded JSON body.
        """
        return self.client.get(reverse("map.pins.meta")).json()

    def test_deleting_the_oldest_pin_changes_the_fingerprint(self) -> None:
        before = self._meta()

        self.pins[0].delete()

        after = self._meta()
        self.assertNotEqual(after["fingerprint"], before["fingerprint"])

    def test_the_timestamp_alone_could_not_have_seen_it(self) -> None:
        """The reason the fingerprint exists, asserted rather than assumed.

        If this ever fails, deleting a pin *does* move `Max(updated)` on its own and the pairing above is
        redundant - which would be worth knowing before anyone simplifies it away."""
        before = self._meta()

        self.pins[0].delete()

        self.assertEqual(self._meta()["last_updated"], before["last_updated"])

    def test_the_reported_timestamp_is_still_a_timestamp(self) -> None:
        from django.utils.dateparse import parse_datetime

        self.assertIsNotNone(parse_datetime(self._meta()["last_updated"]))

    def test_an_account_with_no_pins_has_a_stable_fingerprint(self) -> None:
        empty = baker.make(User).profile

        state = pin_collection_state(empty)

        self.assertEqual(state.total, 0)
        self.assertIsNone(state.last_updated)
        self.assertEqual(state.fingerprint, pin_collection_state(empty).fingerprint)

    def test_creating_a_pin_changes_it(self) -> None:
        before = pin_collection_state(self.profile).fingerprint

        baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=99.0, longitude=9.0))

        self.assertNotEqual(pin_collection_state(self.profile).fingerprint, before)

    def test_deleting_one_pin_and_creating_another_changes_it(self) -> None:
        """The case where the count comes back to where it started."""
        before = pin_collection_state(self.profile).fingerprint

        self.pins[0].delete()
        baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=98.0, longitude=8.0))

        self.assertNotEqual(pin_collection_state(self.profile).fingerprint, before)
