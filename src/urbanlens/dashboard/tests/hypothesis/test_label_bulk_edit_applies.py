"""What the Organize page's bulk label editor writes, and to which labels (P37)."""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.labels import clean_color
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class _BulkEditCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _tag(self, name: str, **fields) -> Label:
        return Label.objects.create(profile=self.profile, kind=KIND_TAG, name=name, **fields)

    def _post(self, body: dict, url_kind: str = "tags"):
        return self.client.post(
            reverse("label.bulk_edit", kwargs={"label_kind": url_kind}),
            data=json.dumps(body),
            content_type="application/json",
        )


class FieldsTests(_BulkEditCase):
    def test_only_the_fields_sent_are_written(self) -> None:
        first = self._tag("ZzBulk One", description="first notes", color="#111111")
        second = self._tag("ZzBulk Two", description="second notes", color="#222222")

        response = self._post({"ids": [first.pk, second.pk], "color": "#ff0000"})

        self.assertEqual(response.status_code, 200)
        for label, notes in ((first, "first notes"), (second, "second notes")):
            label.refresh_from_db()
            self.assertEqual(label.color, clean_color("#ff0000"))
            self.assertEqual(label.description, notes, "a field the request never named was overwritten")

    def test_labels_outside_the_requesters_own_kind_are_untouched(self) -> None:
        mine = self._tag("ZzBulk Mine", color="#111111")
        my_category = Label.objects.create(profile=self.profile, kind=KIND_CATEGORY, name="ZzBulk Cat", color="#111111")
        other_profile = Profile.objects.get(user=baker.make("auth.User"))
        theirs = Label.objects.create(profile=other_profile, kind=KIND_TAG, name="ZzBulk Theirs", color="#111111")

        self._post({"ids": [mine.pk, my_category.pk, theirs.pk], "color": "#ff0000"})

        mine.refresh_from_db()
        self.assertEqual(mine.color, clean_color("#ff0000"), "the edit did not run, so the rest proves nothing")
        for untouched in (my_category, theirs):
            untouched.refresh_from_db()
            self.assertEqual(untouched.color, "#111111")

    def test_a_protected_status_is_skipped(self) -> None:
        protected = Label.objects.create(
            profile=self.profile, kind=KIND_STATUS, name="ZzBulk Locked", color="#111111", is_protected=True
        )
        editable = Label.objects.create(profile=self.profile, kind=KIND_STATUS, name="ZzBulk Open", color="#111111")

        self._post({"ids": [protected.pk, editable.pk], "color": "#ff0000"}, url_kind="statuses")

        protected.refresh_from_db()
        editable.refresh_from_db()
        self.assertEqual(editable.color, clean_color("#ff0000"))
        self.assertEqual(protected.color, "#111111")


class HierarchyTests(_BulkEditCase):
    def test_parents_are_added_but_never_one_that_would_make_a_cycle(self) -> None:
        parent = self._tag("ZzBulk Parent")
        child = self._tag("ZzBulk Child")
        child.parents.add(parent)
        unrelated = self._tag("ZzBulk Unrelated")

        response = self._post({"ids": [parent.pk], "add_parent_ids": [child.pk, unrelated.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(parent.parents.all()), {unrelated}, "the child became its own grandparent")

    def test_children_are_added_to_every_selected_label(self) -> None:
        first = self._tag("ZzBulk First")
        second = self._tag("ZzBulk Second")
        child = self._tag("ZzBulk Kid")

        self._post({"ids": [first.pk, second.pk], "add_child_ids": [child.pk]})

        self.assertEqual(set(child.parents.all()), {first, second})
