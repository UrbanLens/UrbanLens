"""LabelMergeView's GET form had zero rendering coverage - only a service-level
test suite (test_label_merge_service.py) and a single 404-for-media check
(test_media_labels.py) existed. This covers the search + button-list target
picker (organize_label_merge_form.html) that replaced the native <select>.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label


class LabelMergeTargetPickerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_get_renders_search_and_button_list_for_each_candidate(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="tag", name="Source")
        target = baker.make(Label, profile=self.profile, kind="tag", name="Target One")

        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="merge-target-search"')
        self.assertContains(response, 'id="merge-target-list"')
        self.assertContains(response, 'id="merge-target-id"')
        self.assertContains(response, f'data-id="{target.id}"')
        self.assertContains(response, "Target One")
        self.assertNotContains(response, "merge-target-select")
        self.assertNotContains(response, "<select")

    def test_submit_button_starts_disabled(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="category", name="Source")
        baker.make(Label, profile=self.profile, kind="category", name="Target")

        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "category", "label_id": source.id}))

        self.assertContains(response, 'id="merge-submit-btn" disabled')

    def test_empty_state_shown_when_no_other_candidates_exist(self) -> None:
        # Every profile starts with a seeded set of default tags, so an empty
        # candidate list only occurs once those are also excluded.
        source = baker.make(Label, profile=self.profile, kind="tag", name="Only One")
        Label.objects.filter(kind="tag").exclude(id=source.id).delete()

        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No other tag to merge into.")
