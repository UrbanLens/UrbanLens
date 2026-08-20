"""A Merge button is only rendered where merging actually works.

`_organize_label_card.html` had a second branch: when no `merge_url` was passed
and the kind was `people`, it rendered a Merge button calling
`peopleMergeSingle(...)`. That function is defined nowhere in the repository, so
every click raised `ReferenceError` and nothing happened.

Wiring it to the real route would not have helped: `KIND_USER` and `KIND_MEDIA`
both set `enable_single_merge=False`, and `LabelMergeView` answers **404** for a
kind that does. The affordance was for a capability the server refuses, so the
fix is to not offer it.

This test is written against the *config* rather than the template so it keeps
holding if the markup moves: whatever the page renders, a kind that cannot
single-merge must not show a single-merge control.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.labels import _config
from urbanlens.dashboard.models.labels.meta import KIND_MEDIA, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class SingleMergeAffordanceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _rows_html(self, kind: str) -> str:
        response = self.client.get(reverse("label.rows", kwargs={"label_kind": _config(kind).url_kind}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_kinds_without_single_merge_really_refuse_it(self) -> None:
        """The premise: a Merge button on these kinds could only ever 404."""
        for kind in (KIND_USER, KIND_MEDIA):
            with self.subTest(kind=kind):
                self.assertFalse(_config(kind).enable_single_merge)

                label = baker.make(Label, profile=self.profile, kind=kind, name=f"Subject {kind}")
                response = self.client.get(reverse("label.merge", kwargs={"label_kind": _config(kind).url_kind, "label_id": label.pk}))

                self.assertEqual(response.status_code, 404)

    def test_a_people_label_row_offers_no_merge_control(self) -> None:
        baker.make(Label, profile=self.profile, kind=KIND_USER, name="Alex")

        html = self._rows_html(KIND_USER)

        self.assertNotIn("peopleMergeSingle", html, "the handler this called does not exist anywhere in the codebase")
        self.assertNotIn('title="Merge"', html, "a control for something the server answers 404 to")

    def test_a_kind_that_does_support_single_merge_still_offers_it(self) -> None:
        """Otherwise this test would pass by deleting the feature everywhere."""
        tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Rooftop")

        html = self._rows_html(KIND_TAG)

        self.assertTrue(_config(KIND_TAG).enable_single_merge)
        self.assertIn(reverse("label.merge", kwargs={"label_kind": _config(KIND_TAG).url_kind, "label_id": tag.pk}), html)
