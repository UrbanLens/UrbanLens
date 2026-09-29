"""A merge that also renames or restyles its target changes only those fields, in the same request as the merge."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label


class MergeWithEditsTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _tag(self, name: str, **fields) -> Label:
        return Label.objects.create(profile=self.profile, kind=KIND_TAG, name=name, **fields)

    def _merge(self, target: Label, sources: list[Label], **edits: str):
        return self.client.post(
            reverse("label.multi_merge", kwargs={"label_kind": "tag"}),
            data=json.dumps({"target_id": target.id, "source_ids": [s.id for s in sources], **edits}),
            content_type="application/json",
        )

    def test_a_rename_keeps_the_targets_description_parents_children_and_auto_tag_choice(self) -> None:
        parent = self._tag("Parent")
        child = self._tag("Child")
        target = self._tag("Target", description="Kept", allow_auto_tag=False)
        target.parents.add(parent)
        target.children.add(child)
        source = self._tag("Source")

        response = self._merge(target, [source], name="Renamed", icon="home", color="#112233")

        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertEqual((target.name, target.icon, target.color), ("Renamed", "home", "#112233"))
        self.assertEqual(target.description, "Kept")
        self.assertFalse(target.allow_auto_tag)
        self.assertEqual(list(target.parents.values_list("id", flat=True)), [parent.id])
        self.assertEqual(list(target.children.values_list("id", flat=True)), [child.id])
        self.assertFalse(Label.objects.filter(id=source.id).exists())

    def test_the_target_may_take_a_merged_sources_name(self) -> None:
        target = self._tag("Target")
        source = self._tag("Source")

        response = self._merge(target, [source], name="Source")

        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.name, "Source")

    def test_a_name_another_label_holds_refuses_the_whole_merge(self) -> None:
        self._tag("Taken")
        target = self._tag("Target")
        source = self._tag("Source")

        response = self._merge(target, [source], name="Taken")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Label.objects.filter(id=source.id).exists())
        target.refresh_from_db()
        self.assertEqual(target.name, "Target")

    def test_a_shared_target_cannot_be_renamed_by_a_merge(self) -> None:
        shared = Label.objects.create(profile=None, kind=KIND_TAG, name="Shared")
        source = self._tag("Source")

        response = self._merge(shared, [source], name="Hijacked")

        self.assertEqual(response.status_code, 400)
        shared.refresh_from_db()
        self.assertEqual(shared.name, "Shared")
        self.assertTrue(Label.objects.filter(id=source.id).exists())
