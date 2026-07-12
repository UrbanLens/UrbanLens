"""Tests for pin deletion via ``DELETE /rest/pins/<uuid>/``.

Covers ownership scoping, the sub-pin decision handshake (409 until the
client says whether children are deleted or kept), keep-mode promotion
(including root-slot conflicts), and undo restoration of full subtrees at
arbitrary nesting depth.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.undo.service import restore_undo_action

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "pin-delete-tests"}}


def _delete_url(pin: Pin) -> str:
    return f"/dashboard/rest/pins/{pin.uuid}/"


@override_settings(CACHES=_LOCMEM_CACHES)
class PinDeleteTests(TestCase):
    """Deleting a pin without descendants behaves as a plain delete."""

    def setUp(self) -> None:
        self.owner = baker.make(User)
        self.profile = self.owner.profile
        self.pin = baker.make(Pin, profile=self.profile, name="My Pin")
        self.client.force_login(self.owner)

    def test_owner_delete_returns_204(self) -> None:
        response = self.client.delete(_delete_url(self.pin))
        self.assertEqual(response.status_code, 204)

    def test_owner_delete_removes_pin_from_db(self) -> None:
        pin_pk = self.pin.pk
        self.client.delete(_delete_url(self.pin))
        self.assertFalse(Pin.objects.filter(pk=pin_pk).exists())

    def test_other_user_delete_is_rejected(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self.client.delete(_delete_url(self.pin))
        self.assertIn(response.status_code, (403, 404))
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_deleting_one_pin_leaves_others_intact(self) -> None:
        other_pin = baker.make(Pin, profile=self.profile)
        self.client.delete(_delete_url(self.pin))
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())


@override_settings(CACHES=_LOCMEM_CACHES)
class PinDeleteChildrenDecisionTests(TestCase):
    """A pin with sub pins requires an explicit children decision."""

    def setUp(self) -> None:
        self.owner = baker.make(User)
        self.profile = self.owner.profile
        self.parent = baker.make(Pin, profile=self.profile, name="Parent")
        self.child = baker.make(Pin, profile=self.profile, name="Child", parent_pin=self.parent)
        self.grandchild = baker.make(Pin, profile=self.profile, name="Grandchild", parent_pin=self.child)
        self.client.force_login(self.owner)

    def test_delete_without_decision_returns_409_with_count(self) -> None:
        response = self.client.delete(_delete_url(self.parent))
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertTrue(data["requires_children_decision"])
        self.assertEqual(data["children"], 2)
        self.assertTrue(Pin.objects.filter(pk=self.parent.pk).exists())

    def test_children_delete_removes_whole_subtree(self) -> None:
        response = self.client.delete(_delete_url(self.parent) + "?children=delete")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Pin.objects.filter(pk__in=[self.parent.pk, self.child.pk, self.grandchild.pk]).exists())

    def test_children_keep_promotes_direct_children(self) -> None:
        response = self.client.delete(_delete_url(self.parent) + "?children=keep")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Pin.objects.filter(pk=self.parent.pk).exists())
        self.child.refresh_from_db()
        self.grandchild.refresh_from_db()
        # Direct child becomes a top-level pin; its own subtree stays attached.
        self.assertIsNone(self.child.parent_pin_id)
        self.assertEqual(self.grandchild.parent_pin_id, self.child.pk)

    def test_children_keep_promotes_mid_level_child_to_grandparent(self) -> None:
        response = self.client.delete(_delete_url(self.child) + "?children=keep")
        self.assertEqual(response.status_code, 204)
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.parent.pk)

    def test_children_keep_handles_shared_location_root_conflict(self) -> None:
        # A child at the parent's exact Location can only become top-level once
        # the parent's row is gone (top-level pins are unique per Location).
        conflicted = baker.make(Pin, profile=self.profile, name="Same Spot", parent_pin=self.parent, location=self.parent.location)
        response = self.client.delete(_delete_url(self.parent) + "?children=keep")
        self.assertEqual(response.status_code, 204)
        conflicted.refresh_from_db()
        self.assertIsNone(conflicted.parent_pin_id)

    def test_pin_without_children_needs_no_decision(self) -> None:
        lone = baker.make(Pin, profile=self.profile)
        response = self.client.delete(_delete_url(lone))
        self.assertEqual(response.status_code, 204)


@override_settings(CACHES=_LOCMEM_CACHES)
class PinDeleteUndoTests(TestCase):
    """Undo restores the deleted subtree at every nesting level."""

    def setUp(self) -> None:
        self.owner = baker.make(User)
        self.profile = self.owner.profile
        self.client.force_login(self.owner)

    def _undo_latest(self) -> list[Pin]:
        action = UndoAction.objects.filter(profile=self.profile, model_label="pin").latest("created")
        return restore_undo_action(action)

    def test_undo_restores_three_level_subtree(self) -> None:
        parent = baker.make(Pin, profile=self.profile, name="Root")
        child = baker.make(Pin, profile=self.profile, name="Mid", parent_pin=parent)
        baker.make(Pin, profile=self.profile, name="Leaf", parent_pin=child)

        self.client.delete(_delete_url(parent) + "?children=delete")
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())

        restored = self._undo_latest()
        self.assertEqual(len(restored), 3)
        new_root = Pin.objects.get(profile=self.profile, name="Root")
        new_mid = Pin.objects.get(profile=self.profile, name="Mid")
        new_leaf = Pin.objects.get(profile=self.profile, name="Leaf")
        self.assertIsNone(new_root.parent_pin_id)
        self.assertEqual(new_mid.parent_pin_id, new_root.pk)
        self.assertEqual(new_leaf.parent_pin_id, new_mid.pk)

    def test_undo_of_sub_pin_reattaches_to_surviving_parent(self) -> None:
        parent = baker.make(Pin, profile=self.profile, name="Root")
        child = baker.make(Pin, profile=self.profile, name="Mid", parent_pin=parent)

        self.client.delete(_delete_url(child))
        self.assertTrue(Pin.objects.filter(pk=parent.pk).exists())

        restored = self._undo_latest()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].parent_pin_id, parent.pk)

    def test_undo_after_keep_restores_only_the_parent(self) -> None:
        parent = baker.make(Pin, profile=self.profile, name="Root")
        child = baker.make(Pin, profile=self.profile, name="Mid", parent_pin=parent)

        self.client.delete(_delete_url(parent) + "?children=keep")
        restored = self._undo_latest()

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Root")
        child.refresh_from_db()
        # The kept (promoted) child stays a top-level pin - keep is permanent.
        self.assertIsNone(child.parent_pin_id)
