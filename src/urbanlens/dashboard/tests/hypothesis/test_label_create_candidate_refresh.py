"""Regression coverage for the Organize page's "create label" dialog.

Creating a label used to leave the still-open dialog's parent/child picker
showing only the candidates that existed at page load - a second label
created in the same session never appeared as a pickable parent/child until
the page was refreshed. ``LabelCreateView`` now appends OOB suggestion
buttons for the freshly-created label into that dialog's picker, keyed to
its ``new-<ns>`` instance id (see ``organize.py``'s ``_KindConfig.select_data_name``
and ``dashboard/partials/ui/_label_rel_new_candidate_oob.html``).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import KIND_TAG, KIND_USER, Label


class LabelCreateCandidateRefreshTests(TestCase):
    """A newly-created label is immediately offered as a parent/child candidate."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_create_tag_appends_oob_suggestion_for_new_tag_dialog(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}),
            data={"name": "Road Trip"},
        )
        self.assertEqual(response.status_code, 200)
        label = Label.objects.get(profile=self.profile, kind=KIND_TAG, name="Road Trip")
        content = response.content.decode()

        # Appended once per relationship direction, targeting the *create*
        # dialog's picker (instance id "new-tag") - not the bulk-edit dialog's.
        self.assertIn('hx-swap-oob="beforeend:#new-tag-suggestions-parent"', content)
        self.assertIn('hx-swap-oob="beforeend:#new-tag-suggestions-child"', content)
        self.assertIn(f'data-id="{label.id}"', content)
        self.assertIn("Road Trip", content)

    def test_create_category_targets_the_cat_namespace_not_the_url_kind(self) -> None:
        # Category is the one kind where the picker namespace ("cat") diverges
        # from the URL kind ("category") - a naive f"new-{label_kind}" would
        # target a picker that doesn't exist.
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "category"}),
            data={"name": "Drive-In Theater"},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('hx-swap-oob="beforeend:#new-cat-suggestions-parent"', content)
        self.assertIn('hx-swap-oob="beforeend:#new-cat-suggestions-child"', content)
        self.assertNotIn("new-category-suggestions", content)

    def test_people_label_suggestion_omits_the_kind_badge(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "people"}),
            data={"name": "Frequent Explorer"},
        )
        self.assertEqual(response.status_code, 200)
        label = Label.objects.get(profile=self.profile, kind=KIND_USER, name="Frequent Explorer")
        content = response.content.decode()
        self.assertIn('hx-swap-oob="beforeend:#new-people-suggestions-parent"', content)
        self.assertNotIn(f"label-kind-chip label-kind-chip--{label.kind}", content)
