"""
LabelMergeView's GET form had zero rendering coverage - only a service-level
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
        self.assertContains(response, f'type="radio" name="target_label_id" value="{target.id}"')
        self.assertContains(response, "Target One")
        self.assertNotContains(response, "merge-target-select")
        self.assertNotContains(response, "<select")

    def test_submit_button_starts_disabled(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="category", name="Source")
        baker.make(Label, profile=self.profile, kind="category", name="Target")

        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "category", "label_id": source.id}))

        self.assertContains(
            response, 'type="submit" id="merge-submit-btn" data-enabled-by="merge-target-list" disabled'
        )

    def test_the_merge_is_confirmed_by_htmx_naming_the_label(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="tag", name="Old Mill")
        baker.make(Label, profile=self.profile, kind="tag", name="Mill")

        html = self.client.get(
            reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id})
        ).content.decode()

        self.assertIn('hx-confirm="Merge &quot;Old Mill&quot; into the selected tag? This cannot be undone."', html)
        self.assertIn('data-confirm-title="Merge Tag"', html)
        self.assertNotIn("onclick=", html)

    def test_empty_state_shown_when_no_other_candidates_exist(self) -> None:
        # Every profile starts with a seeded set of default tags, so an empty
        # candidate list only occurs once those are also excluded.
        source = baker.make(Label, profile=self.profile, kind="tag", name="Only One")
        Label.objects.filter(kind="tag").exclude(id=source.id).delete()

        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No other tag to merge into.")

    def test_a_near_duplicate_is_listed_first_and_chosen(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="tag", name="hospitals")
        baker.make(Label, profile=self.profile, kind="tag", name="Asylum", icon="🏚️")
        plain = baker.make(Label, profile=self.profile, kind="tag", name="Hospital ", icon="")
        iconic = baker.make(Label, profile=self.profile, kind="tag", name="Hospital", icon="🏥")

        html = self.client.get(
            reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id})
        ).content.decode()

        self.assertIn(f'value="{iconic.id}" class="tag-dialog-choice" checked', html)
        self.assertNotIn(f'value="{plain.id}" class="tag-dialog-choice" checked', html)
        self.assertLess(html.index(f'value="{iconic.id}"'), html.index(f'value="{plain.id}"'))
        self.assertIn('data-enabled-by="merge-target-list" class=', html)

    def test_nothing_is_chosen_when_no_name_is_close(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="tag", name="Rooftop")
        baker.make(Label, profile=self.profile, kind="tag", name="Hospital", icon="🏥")

        html = self.client.get(
            reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.id})
        ).content.decode()

        self.assertNotIn("checked", html)
        self.assertIn('data-enabled-by="merge-target-list" disabled', html)
