"""Tests for the map's multi-select bulk actions: delete+undo, merge, bulk edit."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
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

    def test_a_child_pin_can_be_deleted_directly(self) -> None:
        """The multi-select tool can select child pins now, so bulk actions must accept them."""
        response = self._delete([str(self.child.uuid)])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Pin.objects.filter(pk=self.child.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin_a.pk).exists())


@override_settings(CACHES=_LOCMEM_CACHES)
class PinBulkUndoViewTests(TestCase):
    """POST /map/pins/bulk-undo/ recreates pins stashed by a prior bulk delete."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.parent = baker.make(Pin, profile=self.profile, name="Parent")
        self.child = baker.make(Pin, profile=self.profile, parent_pin=self.parent, name="Child")
        self.label = baker.make(Label, kind=KIND_TAG, profile=self.profile)
        self.parent.labels.add(self.label)

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

    def test_restores_labels(self) -> None:
        token = self._delete_and_get_token()
        self._undo(token)
        restored_parent = Pin.objects.get(profile=self.profile, name="Parent")
        self.assertIn(self.label, restored_parent.labels.all())

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

    def test_re_merging_a_pins_own_existing_child_is_a_harmless_no_op(self) -> None:
        """target is already source's parent here - would_create_cycle can never
        reject this merge endpoint's own loop because the target is always root
        (promoted first if needed) by the time each source is checked, so it has
        no ancestor chain left to find a cycle in. Confirms that's genuinely safe."""
        self.target.parent_pin = self.source_a
        self.target.save(update_fields=["parent_pin"])
        response = self._merge(str(self.source_a.uuid), [str(self.target.uuid)])
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.parent_pin_id, self.source_a.pk)

    def test_a_child_pin_can_be_used_as_a_merge_source(self) -> None:
        """The multi-select tool can select child pins now, so bulk actions must accept them."""
        response = self._merge(str(self.target.uuid), [str(self.grandchild.uuid)])
        self.assertEqual(response.status_code, 200)
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.target.pk)

    def test_a_child_pin_can_be_used_as_a_merge_target(self) -> None:
        """Picking a sub pin as the merge target promotes it to top-level first."""
        response = self._merge(str(self.grandchild.uuid), [str(self.source_b.uuid)])
        self.assertEqual(response.status_code, 200)
        self.grandchild.refresh_from_db()
        self.source_b.refresh_from_db()
        self.assertIsNone(self.grandchild.parent_pin_id)
        self.assertEqual(self.source_b.parent_pin_id, self.grandchild.pk)

    def test_promoting_a_child_target_rejects_a_location_conflict(self) -> None:
        conflicting_root = baker.make(Pin, profile=self.profile, location=self.grandchild.location)
        response = self._merge(str(self.grandchild.uuid), [str(self.source_b.uuid)])
        self.assertEqual(response.status_code, 400)
        self.grandchild.refresh_from_db()
        conflicting_root.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.source_a.pk)
        self.assertIsNone(conflicting_root.parent_pin_id)


class PinBulkEditViewTests(TestCase):
    """POST /map/pins/bulk-edit/ replaces description and adds/removes labels in bulk."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile, description="old a")
        self.pin_b = baker.make(Pin, profile=self.profile, description="old b")
        self.tag_present = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="present")
        self.tag_absent = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="absent")
        self.pin_a.labels.add(self.tag_present)

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

    def test_adds_label_to_all_selected_pins(self) -> None:
        new_label = baker.make(Label, kind=KIND_CATEGORY, profile=self.profile)
        self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "add_label_ids": [new_label.id]})
        self.assertIn(new_label, self.pin_a.labels.all())
        self.assertIn(new_label, self.pin_b.labels.all())

    def test_remove_ignores_label_not_present_on_any_selected_pin(self) -> None:
        """The server must re-validate remove_label_ids, not trust the client's list."""
        self._edit({
            "uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)],
            "remove_label_ids": [self.tag_absent.id],
        })
        # tag_absent was never on either pin - nothing should change, and no error either.
        self.assertNotIn(self.tag_absent, self.pin_a.labels.all())

    def test_remove_label_present_on_selection_is_removed(self) -> None:
        self._edit({"uuids": [str(self.pin_a.uuid)], "remove_label_ids": [self.tag_present.id]})
        self.assertNotIn(self.tag_present, self.pin_a.labels.all())

    def test_sets_parent_pin_on_all_selected_pins(self) -> None:
        parent = baker.make(Pin, profile=self.profile)
        response = self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "parent_uuid": str(parent.uuid)})
        self.pin_a.refresh_from_db()
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_a.parent_pin_id, parent.pk)
        self.assertEqual(self.pin_b.parent_pin_id, parent.pk)
        self.assertEqual(response.json()["reparented"], 2)

    def test_leaves_parent_unset_when_parent_uuid_absent(self) -> None:
        self._edit({"uuids": [str(self.pin_a.uuid)]})
        self.pin_a.refresh_from_db()
        self.assertIsNone(self.pin_a.parent_pin_id)

    def test_skips_reparenting_that_would_create_a_cycle(self) -> None:
        child = baker.make(Pin, profile=self.profile, parent_pin=self.pin_a)
        response = self._edit({"uuids": [str(self.pin_a.uuid)], "parent_uuid": str(child.uuid)})
        self.pin_a.refresh_from_db()
        self.assertIsNone(self.pin_a.parent_pin_id)
        self.assertEqual(response.json()["reparented"], 0)

    def test_a_child_pin_can_be_bulk_edited(self) -> None:
        """The multi-select tool can select child pins now, so bulk actions must accept them."""
        parent = baker.make(Pin, profile=self.profile)
        child = baker.make(Pin, profile=self.profile, parent_pin=parent)
        response = self._edit({"uuids": [str(child.uuid)], "description": "child note"})
        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.description, "child note")


class PinBulkEditLabelOptionsViewTests(TestCase):
    """GET /map/pins/bulk-edit/label-options/ only offers labels present on the selection."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile)
        self.pin_b = baker.make(Pin, profile=self.profile)
        self.tag_on_a = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="on-a")
        self.tag_unused = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="unused")
        self.pin_a.labels.add(self.tag_on_a)

    def test_only_includes_labels_present_on_the_selection(self) -> None:
        response = self.client.get(
            reverse("pin.bulk_edit.label_options"),
            {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)]},
        )
        ids = {b["id"] for b in response.json()["labels"]}
        self.assertIn(self.tag_on_a.id, ids)
        self.assertNotIn(self.tag_unused.id, ids)


class PinParentSearchViewTests(TestCase):
    """GET /map/pins/parent-search/ finds the requester's own pins by name or alias."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill")

    def _search(self, params: dict):
        return self.client.get(reverse("pin.parent_search"), params)

    def test_finds_pin_by_name(self) -> None:
        response = self._search({"q": "Old Mill"})
        uuids = {r["uuid"] for r in response.json()["results"]}
        self.assertIn(str(self.pin.uuid), uuids)

    def test_finds_pin_by_alias(self) -> None:
        from urbanlens.dashboard.models.aliases.model import PinAlias

        PinAlias.objects.create(pin=self.pin, name="The Sawmill")
        response = self._search({"q": "Sawmill"})
        uuids = {r["uuid"] for r in response.json()["results"]}
        self.assertIn(str(self.pin.uuid), uuids)

    def test_excludes_uuids_passed_via_exclude_param(self) -> None:
        response = self._search({"q": "Old Mill", "exclude": str(self.pin.uuid)})
        uuids = {r["uuid"] for r in response.json()["results"]}
        self.assertNotIn(str(self.pin.uuid), uuids)

    def test_excludes_other_users_pins(self) -> None:
        other = baker.make(User)
        other_pin = baker.make(Pin, profile=other.profile, name="Old Mill")
        response = self._search({"q": "Old Mill"})
        uuids = {r["uuid"] for r in response.json()["results"]}
        self.assertNotIn(str(other_pin.uuid), uuids)

    def test_short_query_returns_no_results(self) -> None:
        response = self._search({"q": "O"})
        self.assertEqual(response.json()["results"], [])
