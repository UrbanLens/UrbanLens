"""Tests for the map's multi-select bulk actions: delete+undo, merge, bulk edit."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
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
        """Picking a child pin as the merge target promotes it to top-level first."""
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
    """POST /map/pins/bulk-edit/ updates shared pin fields in bulk."""

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

    def test_sets_detail_pin_visual_style_on_every_selected_pin(self) -> None:
        response = self._edit(
            {
                "uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)],
                "icon": "door_front",
                "color": "#2196F3",
                "bg_color": "#FFFFFF",
                "bg_opacity": 45,
                "border_color": "#000000",
                "border_opacity": 70,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.pin_a.refresh_from_db()
        self.pin_b.refresh_from_db()
        for pin in (self.pin_a, self.pin_b):
            self.assertEqual(pin.icon, "door_front")
            self.assertEqual(pin.color, "#2196F3")
            self.assertEqual(pin.detail_bg_color, "#FFFFFF")
            self.assertEqual(pin.detail_bg_opacity, 45)
            self.assertEqual(pin.detail_border_color, "#000000")
            self.assertEqual(pin.detail_border_opacity, 70)

    def test_visual_fields_are_partial_and_can_be_cleared(self) -> None:
        self.pin_a.icon = "warning"
        self.pin_a.color = "#F44336"
        self.pin_a.detail_bg_color = "#FFFFFF"
        self.pin_a.detail_bg_opacity = 80
        self.pin_a.save()

        response = self._edit({"uuids": [str(self.pin_a.uuid)], "icon": None, "bg_opacity": 0})

        self.assertEqual(response.status_code, 200)
        self.pin_a.refresh_from_db()
        self.assertIsNone(self.pin_a.icon)
        self.assertEqual(self.pin_a.color, "#F44336")
        self.assertEqual(self.pin_a.detail_bg_color, "#FFFFFF")
        self.assertEqual(self.pin_a.detail_bg_opacity, 0)

    def test_rejects_visual_opacity_outside_percentage_range(self) -> None:
        response = self._edit({"uuids": [str(self.pin_a.uuid)], "bg_opacity": 101})

        self.assertEqual(response.status_code, 400)
        self.pin_a.refresh_from_db()
        self.assertEqual(self.pin_a.detail_bg_opacity, 80)

    def test_sets_rating_on_all_selected_pins(self) -> None:
        """rating lives on Review (one per profile/pin pair), not a plain Pin field."""
        from urbanlens.dashboard.models.reviews.model import Review

        self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "rating": 4})

        self.assertEqual(Review.objects.for_pair(self.profile, self.pin_a).first().rating, 4)
        self.assertEqual(Review.objects.for_pair(self.profile, self.pin_b).first().rating, 4)

    def test_rating_overwrites_an_existing_review(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        Review.objects.update_or_create(profile=self.profile, pin=self.pin_a, defaults={"rating": 2})

        self._edit({"uuids": [str(self.pin_a.uuid)], "rating": 5})

        self.assertEqual(Review.objects.for_pair(self.profile, self.pin_a).first().rating, 5)

    def test_rating_zero_clears_reviews_on_all_selected_pins(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        Review.objects.update_or_create(profile=self.profile, pin=self.pin_a, defaults={"rating": 3})
        Review.objects.update_or_create(profile=self.profile, pin=self.pin_b, defaults={"rating": 3})

        self._edit({"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "rating": 0})

        self.assertFalse(Review.objects.for_pair(self.profile, self.pin_a).exists())
        self.assertFalse(Review.objects.for_pair(self.profile, self.pin_b).exists())

    def test_leaves_rating_untouched_when_absent(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        Review.objects.update_or_create(profile=self.profile, pin=self.pin_a, defaults={"rating": 3})

        self._edit({"uuids": [str(self.pin_a.uuid)], "description": "unrelated change"})

        self.assertEqual(Review.objects.for_pair(self.profile, self.pin_a).first().rating, 3)

    def test_rating_only_applies_to_the_acting_profiles_review(self) -> None:
        """Bulk-editing rating must never touch another user's review of the same pin."""
        from urbanlens.dashboard.models.reviews.model import Review

        other_profile = baker.make(User).profile
        Review.objects.update_or_create(profile=other_profile, pin=self.pin_a, defaults={"rating": 1})

        self._edit({"uuids": [str(self.pin_a.uuid)], "rating": 5})

        self.assertEqual(Review.objects.for_pair(other_profile, self.pin_a).first().rating, 1)
        self.assertEqual(Review.objects.for_pair(self.profile, self.pin_a).first().rating, 5)


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


class PinBulkExportViewTests(TestCase):
    """POST /map/pins/bulk-export/ downloads the selected pins in the chosen format."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make(Pin, profile=self.profile, name="Pin A")
        self.pin_b = baker.make(Pin, profile=self.profile, name="Pin B")

    def _export(self, fmt: str, uuids: list[str]):
        return self.client.post(
            reverse("pin.bulk_export"),
            data={"format": fmt, "uuids": uuids},
        )

    def test_geojson_export_contains_selected_pins(self) -> None:
        response = self._export("geojson", [str(self.pin_a.uuid), str(self.pin_b.uuid)])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        names = {f["properties"]["name"] for f in data["features"]}
        self.assertEqual(names, {"Pin A", "Pin B"})

    def test_sets_content_disposition_with_matching_extension(self) -> None:
        response = self._export("kml", [str(self.pin_a.uuid)])
        self.assertIn('filename="pins.kml"', response["Content-Disposition"])
        self.assertEqual(response["Content-Type"], "application/vnd.google-earth.kml+xml")

    def test_csv_export_contains_selected_pins(self) -> None:
        response = self._export("csv", [str(self.pin_a.uuid)])
        text = response.content.decode()
        self.assertIn("Pin A", text)
        self.assertNotIn("Pin B", text)

    def test_gpx_export_contains_selected_pins(self) -> None:
        response = self._export("gpx", [str(self.pin_a.uuid)])
        self.assertIn(b"Pin A", response.content)

    def test_unknown_format_returns_400(self) -> None:
        response = self._export("shapefile", [str(self.pin_a.uuid)])
        self.assertEqual(response.status_code, 400)

    def test_missing_uuids_returns_400(self) -> None:
        response = self._export("csv", [])
        self.assertEqual(response.status_code, 400)

    def test_excludes_other_users_pins(self) -> None:
        other_user = baker.make(User)
        other_pin = baker.make(Pin, profile=other_user.profile, name="Not Mine")
        response = self._export("csv", [str(other_pin.uuid)])
        self.assertEqual(response.status_code, 404)

    def test_only_exports_pins_owned_by_the_requester(self) -> None:
        other_user = baker.make(User)
        other_pin = baker.make(Pin, profile=other_user.profile, name="Not Mine")
        response = self._export("csv", [str(self.pin_a.uuid), str(other_pin.uuid)])
        text = response.content.decode()
        self.assertIn("Pin A", text)
        self.assertNotIn("Not Mine", text)

    def test_a_child_pin_can_be_exported(self) -> None:
        """The multi-select tool can select child pins now, so export must accept them."""
        child = baker.make(Pin, profile=self.profile, parent_pin=self.pin_a, name="Child")
        response = self._export("csv", [str(child.uuid)])
        self.assertEqual(response.status_code, 200)
        self.assertIn("Child", response.content.decode())


@override_settings(CACHES=_LOCMEM_CACHES)
class BulkSelectionSizeLimitTests(TestCase):
    """The website's bulk write endpoints bound the selection, like the API's do.

    Every external-API bulk endpoint already declares `max_length=500` on its
    uuid list. The endpoints the map's select tool drives had no bound at all,
    and these edits cannot collapse to one UPDATE: `Pin` carries eight live
    `post_save` receivers, so each selected pin needs a real `save()` -
    measured at ~2 queries per pin for a style edit and ~7 for a rating. An
    unbounded selection therefore turns one click into tens of thousands of
    queries in a single request.

    Read paths are deliberately left unbounded - export's own docstring says it
    uses a form POST specifically so the pin count is not limited, and it costs
    one query regardless of selection size.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def _uuids(self, count: int) -> list[str]:
        """`count` syntactically valid uuids - the limit is checked before ownership."""
        return [f"00000000-0000-4000-8000-{index:012d}" for index in range(count)]

    def _post(self, url_name: str, payload: dict):
        return self.client.post(reverse(url_name), data=json.dumps(payload), content_type="application/json")

    def test_bulk_edit_refuses_an_over_large_selection(self) -> None:
        response = self._post("pin.bulk_edit", {"uuids": self._uuids(501), "color": "#ff0000"})
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"500", response.content)

    def test_bulk_delete_refuses_an_over_large_selection(self) -> None:
        response = self._post("pin.bulk_delete", {"uuids": self._uuids(501)})
        self.assertEqual(response.status_code, 400)

    def test_bulk_merge_refuses_an_over_large_selection(self) -> None:
        response = self._post("pin.bulk_merge", {"target_uuid": str(self.pin.uuid), "source_uuids": self._uuids(501)})
        self.assertEqual(response.status_code, 400)

    def test_the_limit_itself_is_accepted(self) -> None:
        """Anti-vacuity: 500 must still work, or the tests above prove nothing."""
        uuids = [*self._uuids(499), str(self.pin.uuid)]
        response = self._post("pin.bulk_edit", {"uuids": uuids, "color": "#ff0000"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.color, "#ff0000")

    def test_the_refusal_explains_itself(self) -> None:
        """The message is the response body, which is what the toast now shows."""
        response = self._post("pin.bulk_edit", {"uuids": self._uuids(501)})
        self.assertIn("Select at most 500 pins at a time.", response.content.decode())

    def test_export_is_deliberately_not_limited(self) -> None:
        response = self.client.post(
            reverse("pin.bulk_export"),
            data={"format": "csv", "uuids": [*self._uuids(600), str(self.pin.uuid)]},
        )
        self.assertEqual(response.status_code, 200)
