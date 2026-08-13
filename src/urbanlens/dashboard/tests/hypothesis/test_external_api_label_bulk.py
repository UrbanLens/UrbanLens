"""Tests for the external API's label priority-reorder and bulk delete/edit/convert endpoints.

Unlike the internal ``LabelBulk*View`` family (route-scoped to one kind at a
time via a ``label_kind`` URL segment), these endpoints resolve a uuid batch
that may span kinds - see ``serializers_labels_bulk``'s docstring. The
behavior most worth pinning down: a global or protected label named in the
request is silently dropped rather than refused, matching the pin-bulk
endpoints' "not yours, not fatal" philosophy, while an unresolvable
parent/child uuid is a 400 - the caller asked for something specific and
impossible.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_BASE = "/dashboard/api/external/v1/labels/"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class LabelBulkTestCase(TestCase):
    """Shared fixture: one user with a write-scoped key and a couple of owned labels."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Label bulk client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LABELS_READ.value, ApiKeyScope.LABELS_WRITE.value])
        self.label_a = ensure_label(profile=self.profile, name="Rusty", kind=KIND_TAG)
        self.label_b = ensure_label(profile=self.profile, name="Overgrown", kind=KIND_TAG)

    def _post(self, path: str, payload: dict):
        return self.client.post(f"{_BASE}{path}", data=payload, content_type="application/json", **_bearer(self.raw_key))


class LabelReorderTests(LabelBulkTestCase):
    def test_reorders_owned_labels(self) -> None:
        response = self._post("reorder/", {"uuids": [str(self.label_a.uuid), str(self.label_b.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["reordered"], 2)
        self.assertEqual(body["skipped_global_uuids"], [])
        self.label_a.refresh_from_db()
        self.label_b.refresh_from_db()
        self.assertGreater(self.label_a.order, self.label_b.order)

    def test_global_labels_are_skipped_not_reordered(self) -> None:
        global_label = ensure_label(profile=None, name="Everyone's", kind=KIND_TAG, order=5)
        response = self._post("reorder/", {"uuids": [str(self.label_a.uuid), str(global_label.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["reordered"], 1)
        self.assertEqual(body["skipped_global_uuids"], [str(global_label.uuid)])
        global_label.refresh_from_db()
        self.assertEqual(global_label.order, 5)

    def test_requires_labels_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LABELS_READ.value])
        response = self._post("reorder/", {"uuids": [str(self.label_a.uuid)]})
        self.assertEqual(response.status_code, 403)

    def test_empty_uuids_is_a_400(self) -> None:
        self.assertEqual(self._post("reorder/", {"uuids": []}).status_code, 400)


class LabelBulkDeleteTests(LabelBulkTestCase):
    def test_deletes_owned_labels(self) -> None:
        response = self._post("bulk/delete/", {"uuids": [str(self.label_a.uuid), str(self.label_b.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertFalse(Label.objects.filter(pk__in=[self.label_a.pk, self.label_b.pk]).exists())

    def test_protected_label_is_silently_dropped(self) -> None:
        protected = ensure_label(profile=self.profile, name="Visited", kind=KIND_STATUS, is_protected=True)
        response = self._post("bulk/delete/", {"uuids": [str(self.label_a.uuid), str(protected.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertTrue(Label.objects.filter(pk=protected.pk).exists())

    def test_no_matching_labels_is_a_404(self) -> None:
        other = baker.make(User)
        theirs = ensure_label(profile=Profile.objects.get(user=other), name="Not yours", kind=KIND_TAG)
        response = self._post("bulk/delete/", {"uuids": [str(theirs.uuid)]})
        self.assertEqual(response.status_code, 404)

    def test_requires_labels_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LABELS_READ.value])
        response = self._post("bulk/delete/", {"uuids": [str(self.label_a.uuid)]})
        self.assertEqual(response.status_code, 403)


class LabelBulkEditTests(LabelBulkTestCase):
    def test_sets_icon_and_color_on_every_label(self) -> None:
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid), str(self.label_b.uuid)], "icon": "star", "color": "red"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 2)
        self.label_a.refresh_from_db()
        self.label_b.refresh_from_db()
        self.assertEqual(self.label_a.icon, "star")
        self.assertEqual(self.label_b.color, "red")

    def test_explicit_null_description_clears_it(self) -> None:
        Label.objects.filter(pk=self.label_a.pk).update(description="Old")
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid)], "description": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.label_a.refresh_from_db()
        self.assertIsNone(self.label_a.description)

    def test_absent_fields_are_left_untouched(self) -> None:
        Label.objects.filter(pk=self.label_a.pk).update(icon="keep-me")
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid)], "order": 3})
        self.assertEqual(response.status_code, 200, response.content)
        self.label_a.refresh_from_db()
        self.assertEqual(self.label_a.icon, "keep-me")

    def test_adds_a_parent_to_every_label(self) -> None:
        parent = ensure_label(profile=self.profile, name="Structures", kind=KIND_TAG)
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid), str(self.label_b.uuid)], "add_parent_uuids": [str(parent.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.label_a.parents.filter(pk=parent.pk).exists())
        self.assertTrue(self.label_b.parents.filter(pk=parent.pk).exists())

    def test_unresolvable_parent_uuid_is_a_400(self) -> None:
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid)], "add_parent_uuids": ["00000000-0000-0000-0000-000000000000"]})
        self.assertEqual(response.status_code, 400)

    def test_a_parent_assignment_that_would_cycle_is_skipped(self) -> None:
        self.label_a.parents.add(self.label_b)
        response = self._post("bulk/edit/", {"uuids": [str(self.label_b.uuid)], "add_parent_uuids": [str(self.label_a.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self.label_b.parents.filter(pk=self.label_a.pk).exists())

    def test_protected_label_is_silently_dropped_from_the_batch(self) -> None:
        protected = ensure_label(profile=self.profile, name="Visited", kind=KIND_STATUS, is_protected=True)
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid), str(protected.uuid)], "icon": "star"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)
        protected.refresh_from_db()
        self.assertNotEqual(protected.icon, "star")

    def test_requires_labels_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LABELS_READ.value])
        response = self._post("bulk/edit/", {"uuids": [str(self.label_a.uuid)], "icon": "star"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_add_parent_uuid_rolls_back_earlier_edits_in_the_same_batch(self) -> None:
        response = self._post(
            "bulk/edit/",
            {
                "uuids": [str(self.label_a.uuid)],
                "icon": "star",
                "add_parent_uuids": ["00000000-0000-0000-0000-000000000000"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.label_a.refresh_from_db()
        self.assertNotEqual(self.label_a.icon, "star")


class LabelBulkConvertTests(LabelBulkTestCase):
    def test_converts_tags_to_categories(self) -> None:
        response = self._post("bulk/convert/", {"uuids": [str(self.label_a.uuid), str(self.label_b.uuid)], "target_kind": KIND_CATEGORY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["converted"], 2)
        self.label_a.refresh_from_db()
        self.assertEqual(self.label_a.kind, KIND_CATEGORY)

    def test_status_labels_cannot_be_converted_away(self) -> None:
        status_label = ensure_label(profile=self.profile, name="Explored", kind=KIND_STATUS)
        response = self._post("bulk/convert/", {"uuids": [str(status_label.uuid)], "target_kind": KIND_TAG})
        self.assertEqual(response.status_code, 404)
        status_label.refresh_from_db()
        self.assertEqual(status_label.kind, KIND_STATUS)

    def test_a_label_already_at_the_target_kind_is_a_no_op(self) -> None:
        response = self._post("bulk/convert/", {"uuids": [str(self.label_a.uuid)], "target_kind": KIND_TAG})
        self.assertEqual(response.status_code, 404)

    def test_conversion_clears_the_labels_parents(self) -> None:
        parent = ensure_label(profile=self.profile, name="Structures", kind=KIND_TAG)
        self.label_a.parents.add(parent)
        response = self._post("bulk/convert/", {"uuids": [str(self.label_a.uuid)], "target_kind": KIND_CATEGORY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.label_a.parents.count(), 0)

    def test_requires_labels_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LABELS_READ.value])
        response = self._post("bulk/convert/", {"uuids": [str(self.label_a.uuid)], "target_kind": KIND_CATEGORY})
        self.assertEqual(response.status_code, 403)
