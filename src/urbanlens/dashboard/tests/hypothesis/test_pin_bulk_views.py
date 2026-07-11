"""Tests for the map's multi-select bulk actions: delete+undo, merge, bulk edit."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import TripActivity

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=_LOCMEM_CACHES)
class PinBulkDeleteViewTests(TestCase):
    """POST /map/pins/bulk-delete/ removes the selected root pins and their subtree."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile)
        self.pin_b = baker.make(Pin, profile=self.profile)
        self.child = baker.make(Pin, profile=self.profile, parent_pin=self.pin_a)

    def _delete(self, uuids: list[str]):
        return self.client.post(
            reverse("pin.bulk_delete"),
            data=json.dumps({"uuids": uuids}),
            content_type="application/json",
        )

    def test_removes_selected_pins(self) -> None:
        self._delete([str(self.pin_a.uuid)])
        self.assertFalse(Pin.objects.filter(pk=self.pin_a.pk).exists())

    def test_cascades_to_descendant_subtree(self) -> None:
        self._delete([str(self.pin_a.uuid)])
        self.assertFalse(Pin.objects.filter(pk=self.child.pk).exists())

    def test_leaves_other_pins_untouched(self) -> None:
        self._delete([str(self.pin_a.uuid)])
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())

    def test_returns_undo_token_and_count(self) -> None:
        response = self._delete([str(self.pin_a.uuid)])
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["undo_token"])
        self.assertEqual(data["count"], 1)

    def test_excludes_other_users_pins(self) -> None:
        other_user = baker.make(User)
        other_pin = baker.make(Pin, profile=other_user.profile)
        response = self._delete([str(other_pin.uuid)])
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())

    def test_images_survive_orphaned(self) -> None:
        image = baker.make(Image, pin=self.pin_a, profile=self.profile)
        self._delete([str(self.pin_a.uuid)])
        image.refresh_from_db()
        self.assertIsNone(image.pin_id)

    def test_trip_activities_survive_orphaned(self) -> None:
        activity = baker.make(TripActivity, pin=self.pin_a)
        self._delete([str(self.pin_a.uuid)])
        activity.refresh_from_db()
        self.assertIsNone(activity.pin_id)


@override_settings(CACHES=_LOCMEM_CACHES)
class PinBulkUndoViewTests(TestCase):
    """POST /map/pins/bulk-undo/ recreates pins stashed by a prior bulk delete."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.parent = baker.make(Pin, profile=self.profile, name="Parent")
        self.child = baker.make(Pin, profile=self.profile, parent_pin=self.parent, name="Child")
        self.badge = baker.make(Badge, kind=KIND_TAG, profile=self.profile)
        self.parent.badges.add(self.badge)

    def _delete_and_get_token(self) -> str:
        response = self.client.post(
            reverse("pin.bulk_delete"),
            data=json.dumps({"uuids": [str(self.parent.uuid)]}),
            content_type="application/json",
        )
        return response.json()["undo_token"]

    def _undo(self, token: str):
        return self.client.post(
            reverse("pin.bulk_undo"),
            data=json.dumps({"token": token}),
            content_type="application/json",
        )

    def test_restores_pins_with_new_pks(self) -> None:
        old_parent_pk = self.parent.pk
        token = self._delete_and_get_token()
        response = self._undo(token)
        self.assertTrue(response.json()["ok"])
        restored_parent = Pin.objects.get(profile=self.profile, name="Parent")
        self.assertNotEqual(restored_parent.pk, old_parent_pk)

    def test_restores_hierarchy_within_batch(self) -> None:
        token = self._delete_and_get_token()
        self._undo(token)
        restored_parent = Pin.objects.get(profile=self.profile, name="Parent")
        restored_child = Pin.objects.get(profile=self.profile, name="Child")
        self.assertEqual(restored_child.parent_pin_id, restored_parent.pk)

    def test_restores_badges(self) -> None:
        token = self._delete_and_get_token()
        self._undo(token)
        restored_parent = Pin.objects.get(profile=self.profile, name="Parent")
        self.assertIn(self.badge, restored_parent.badges.all())

    def test_expired_or_unknown_token_returns_410(self) -> None:
        response = self._undo("not-a-real-token")
        self.assertEqual(response.status_code, 410)

    def test_undo_consumes_the_token(self) -> None:
        token = self._delete_and_get_token()
        self._undo(token)
        second_response = self._undo(token)
        self.assertEqual(second_response.status_code, 410)

    def test_wrong_profile_cannot_undo_another_users_delete(self) -> None:
        token = self._delete_and_get_token()
        other_user = baker.make(User)
        self.client.force_login(other_user)
        response = self._undo(token)
        self.assertEqual(response.status_code, 410)


class PinBulkMergeViewTests(TestCase):
    """POST /map/pins/bulk-merge/ re-parents source pins under the chosen target."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.target = baker.make(Pin, profile=self.profile)
        self.source_a = baker.make(Pin, profile=self.profile)
        self.source_b = baker.make(Pin, profile=self.profile)
        self.grandchild = baker.make(Pin, profile=self.profile, parent_pin=self.source_a)

    def _merge(self, target_uuid: str, source_uuids: list[str]):
        return self.client.post(
            reverse("pin.bulk_merge"),
            data=json.dumps({"target_uuid": target_uuid, "source_uuids": source_uuids}),
            content_type="application/json",
        )

    def test_sets_parent_pin_on_sources(self) -> None:
        self._merge(str(self.target.uuid), [str(self.source_a.uuid), str(self.source_b.uuid)])
        self.source_a.refresh_from_db()
        self.source_b.refresh_from_db()
        self.assertEqual(self.source_a.parent_pin_id, self.target.pk)
        self.assertEqual(self.source_b.parent_pin_id, self.target.pk)

    def test_preserves_sources_own_subtree(self) -> None:
        self._merge(str(self.target.uuid), [str(self.source_a.uuid)])
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.source_a.pk)

    def test_rejects_merge_that_would_create_a_cycle(self) -> None:
        # Force an already-corrupted setup where target descends from source_a,
        # then attempt to merge source_a into target - would close a loop.
        self.target.parent_pin = self.source_a
        self.target.save(update_fields=["parent_pin"])
        response = self._merge(str(self.source_a.uuid), [str(self.target.uuid)])
        self.assertEqual(response.status_code, 400)

    def test_scoped_to_root_pins_only(self) -> None:
        """A detail pin can't be used as a merge source - it's excluded from selection."""
        response = self._merge(str(self.target.uuid), [str(self.grandchild.uuid)])
        self.assertEqual(response.status_code, 400)
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.source_a.pk)


class PinBulkEditViewTests(TestCase):
    """POST /map/pins/bulk-edit/ replaces description and adds/removes badges in bulk."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile, description="old a")
        self.pin_b = baker.make(Pin, profile=self.profile, description="old b")
        self.tag_present = baker.make(Badge, kind=KIND_TAG, profile=self.profile, name="present")
        self.tag_absent = baker.make(Badge, kind=KIND_TAG, profile=self.profile, name="absent")
        self.pin_a.badges.add(self.tag_present)

    def _edit(self, payload: dict):
        return self.client.post(
            reverse("pin.bulk_edit"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_replaces_description_when_provided(self) -> None:
        self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "description": "new description"})
        self.pin_a.refresh_from_db()
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_a.description, "new description")
        self.assertEqual(self.pin_b.description, "new description")

    def test_leaves_description_when_absent(self) -> None:
        self._edit({"uuids": [str(self.pin_a.uuid)]})
        self.pin_a.refresh_from_db()
        self.assertEqual(self.pin_a.description, "old a")

    def test_adds_badge_to_all_selected_pins(self) -> None:
        new_badge = baker.make(Badge, kind=KIND_CATEGORY, profile=self.profile)
        self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "add_badge_ids": [new_badge.id]})
        self.assertIn(new_badge, self.pin_a.badges.all())
        self.assertIn(new_badge, self.pin_b.badges.all())

    def test_remove_ignores_badge_not_present_on_any_selected_pin(self) -> None:
        """The server must re-validate remove_badge_ids, not trust the client's list."""
        self._edit({
            "uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)],
            "remove_badge_ids": [self.tag_absent.id],
        })
        # tag_absent was never on either pin - nothing should change, and no error either.
        self.assertNotIn(self.tag_absent, self.pin_a.badges.all())

    def test_remove_badge_present_on_selection_is_removed(self) -> None:
        self._edit({"uuids": [str(self.pin_a.uuid)], "remove_badge_ids": [self.tag_present.id]})
        self.assertNotIn(self.tag_present, self.pin_a.badges.all())


class PinBulkEditBadgeOptionsViewTests(TestCase):
    """GET /map/pins/bulk-edit/badge-options/ only offers badges present on the selection."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile)
        self.pin_b = baker.make(Pin, profile=self.profile)
        self.tag_on_a = baker.make(Badge, kind=KIND_TAG, profile=self.profile, name="on-a")
        self.tag_unused = baker.make(Badge, kind=KIND_TAG, profile=self.profile, name="unused")
        self.pin_a.badges.add(self.tag_on_a)

    def test_only_includes_badges_present_on_the_selection(self) -> None:
        response = self.client.get(
            reverse("pin.bulk_edit.badge_options"),
            {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)]},
        )
        ids = {b["id"] for b in response.json()["badges"]}
        self.assertIn(self.tag_on_a.id, ids)
        self.assertNotIn(self.tag_unused.id, ids)
