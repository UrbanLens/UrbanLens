"""Tests for UL-155: label ("badge") kind-change UX.

Converting a label between tag/category/status keeps its pin/wiki
memberships but clears its parent/child hierarchy (LabelEditView.post -
_apply_kind_conversion + label.parents.clear() - a parent/child link only
makes sense between two labels of the same kind, since _parent_candidates
is itself kind-scoped). The edit form's hint text used to only mention
memberships being migrated, saying nothing about hierarchy being lost -
this covers the now-conditional warning and the underlying clear-on-convert
behavior it describes.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label

_HIERARCHY_WARNING = "Its parent/child relationships will be cleared"


class LabelKindChangeHierarchyWarningTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_edit_form_warns_about_losing_hierarchy_when_the_label_has_a_parent(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind="tag", name="Parent")
        child = baker.make(Label, profile=self.profile, kind="tag", name="Child")
        child.parents.add(parent)

        response = self.client.get(reverse("label.edit", kwargs={"label_kind": "tag", "label_id": child.id}))

        self.assertContains(response, _HIERARCHY_WARNING)

    def test_edit_form_warns_about_losing_hierarchy_when_the_label_has_a_child(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind="tag", name="Parent")
        child = baker.make(Label, profile=self.profile, kind="tag", name="Child")
        child.parents.add(parent)

        response = self.client.get(reverse("label.edit", kwargs={"label_kind": "tag", "label_id": parent.id}))

        self.assertContains(response, _HIERARCHY_WARNING)

    def test_edit_form_omits_the_warning_for_a_label_with_no_hierarchy(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Standalone")

        response = self.client.get(reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id}))

        self.assertNotContains(response, _HIERARCHY_WARNING)

    def test_converting_kind_actually_clears_parents(self) -> None:
        """The warning text describes real behavior, not just a UI hint -
        confirm parents are genuinely cleared on a kind conversion."""
        parent = baker.make(Label, profile=self.profile, kind="tag", name="Parent")
        child = baker.make(Label, profile=self.profile, kind="tag", name="Child")
        child.parents.add(parent)

        url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": child.id})
        response = self.client.post(url, data={"name": "Child", "kind": "category"})

        self.assertEqual(response["X-Kind-Changed"], "category")
        child.refresh_from_db()
        self.assertEqual(child.kind, "category")
        self.assertEqual(list(child.parents.all()), [])

    def test_editing_without_changing_kind_preserves_parents(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind="tag", name="Parent")
        child = baker.make(Label, profile=self.profile, kind="tag", name="Child")
        child.parents.add(parent)

        url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": child.id})
        response = self.client.post(url, data={"name": "Child", "kind": "tag", "parent_ids": [parent.id]})

        self.assertNotIn("X-Kind-Changed", response)
        child.refresh_from_db()
        self.assertEqual(list(child.parents.all()), [parent])
