"""`merge_pins` must not delete the survivor when it is the loser's direct child."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.pin_merge import SurvivorRelocationCollisionError, merge_pins


class _PinFixtures(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self._locations = 0

    def location(self):
        self._locations += 1
        return baker.make(
            "dashboard.Location",
            latitude=35.0 + self._locations * 0.01,
            longitude=-80.0 - self._locations * 0.01,
        )


class SurvivorIsLosersDirectChildTests(_PinFixtures):
    """Loser is a root pin; survivor is nested directly beneath it."""

    def test_survivor_is_promoted_to_root_instead_of_deleted(self) -> None:
        loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        survivor = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=loser)

        merge_pins(survivor, loser, self.profile)

        survivor.refresh_from_db()
        self.assertIsNone(survivor.parent_pin_id)
        self.assertFalse(Pin.objects.filter(pk=loser.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=survivor.pk).exists())

    def test_losers_other_children_still_land_on_survivor(self) -> None:
        loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        survivor = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=loser)
        sibling = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=loser)

        merge_pins(survivor, loser, self.profile)

        sibling.refresh_from_db()
        self.assertEqual(sibling.parent_pin_id, survivor.pk)


class SurvivorIsLosersDirectChildWithGrandparentTests(_PinFixtures):
    """Loser itself has a parent; survivor should land there, not at root."""

    def test_survivor_is_reparented_onto_losers_own_parent(self) -> None:
        grandparent = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=grandparent)
        survivor = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=loser)

        merge_pins(survivor, loser, self.profile)

        survivor.refresh_from_db()
        self.assertEqual(survivor.parent_pin_id, grandparent.pk)
        self.assertFalse(Pin.objects.filter(pk=loser.pk).exists())


class SurvivorPromotionCollidesTests(_PinFixtures):
    """Loser is root; survivor's own Location already has another root pin."""

    def test_merge_is_refused_with_the_right_error(self) -> None:
        shared = self.location()
        blocker = baker.make(Pin, profile=self.profile, location=shared, parent_pin=None)
        loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        # Legal while it is a child - the uniqueness constraint on
        # (location, profile) is conditional on parent_pin IS NULL.
        survivor = baker.make(Pin, profile=self.profile, location=shared, parent_pin=loser)

        with self.assertRaises(SurvivorRelocationCollisionError):
            merge_pins(survivor, loser, self.profile)

        for pin in (blocker, loser, survivor):
            self.assertTrue(Pin.objects.filter(pk=pin.pk).exists(), f"pin {pin.pk} was destroyed by a refused merge")
        survivor.refresh_from_db()
        self.assertEqual(survivor.parent_pin_id, loser.pk)
