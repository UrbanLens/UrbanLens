"""A conflicting label's name is escaped in the bulk-convert refusal."""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile

HOSTILE = '<img src=x onerror="alert(1)">'


class LabelConvertEscapesNamesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

        # A tag whose name is hostile, and a category of the same name, so converting
        # tag -> category collides and the refusal names the tag.
        self.tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name=HOSTILE)
        baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name=HOSTILE)

    def test_the_conflict_refusal_does_not_echo_raw_markup(self) -> None:
        response = self.client.post(
            reverse("label.bulk_convert", kwargs={"label_kind": KIND_TAG}),
            data=json.dumps({"ids": [self.tag.pk], "kind": KIND_CATEGORY}),
            content_type="application/json",
        )

        body = response.content.decode()
        self.assertEqual(
            response.status_code, 400, f"expected a conflict refusal, got {response.status_code}: {body[:200]}"
        )
        self.assertNotIn("<img", body, "the refusal echoed the label name as raw markup")
        self.assertIn("&lt;img", body, "the label name should appear escaped, not omitted")
