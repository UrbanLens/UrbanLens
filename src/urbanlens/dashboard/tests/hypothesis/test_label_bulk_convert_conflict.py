"""Bulk-converting a label into a kind that already has that name must not 500.

`Label` is unique on `(lower(name), profile, kind)`. The single create/edit paths
check `find_conflicting_label` first and return a readable message; the bulk-convert
path changes `label.kind` and saves without any check, so converting a tag whose name
already exists as a category violates the constraint.

The names are deliberately distinctive: a new profile is seeded with ~46 default
labels, so ordinary words collide with the fixtures rather than with each other.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile

NAME = "ZzAudit Collide"


class LabelBulkConvertConflictTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _convert(self, label: Label, url_kind: str) -> object:
        return self.client.post(
            reverse("label.bulk_convert", kwargs={"label_kind": url_kind}),
            data=json.dumps({"ids": [label.id]}),
            content_type="application/json",
        )

    def test_converting_onto_an_existing_name_does_not_500(self) -> None:
        tag = Label.objects.create(profile=self.profile, name=NAME, kind=KIND_TAG)
        Label.objects.create(profile=self.profile, name=NAME, kind=KIND_CATEGORY)

        response = self._convert(tag, "tags")

        self.assertLess(response.status_code, 500, "the constraint violation surfaced as a server error")

    def test_the_colliding_label_is_left_alone(self) -> None:
        tag = Label.objects.create(profile=self.profile, name=NAME, kind=KIND_TAG)
        Label.objects.create(profile=self.profile, name=NAME, kind=KIND_CATEGORY)

        self._convert(tag, "tags")

        tag.refresh_from_db()
        self.assertEqual(tag.kind, KIND_TAG, "the tag was converted into a duplicate")
        self.assertEqual(Label.objects.filter(profile=self.profile, name__iexact=NAME).count(), 2)

    def test_a_non_colliding_convert_still_works(self) -> None:
        """Guards the checks above from passing because conversion stopped entirely."""
        tag = Label.objects.create(profile=self.profile, name="ZzAudit Unique", kind=KIND_TAG)

        self._convert(tag, "tags")

        tag.refresh_from_db()
        self.assertEqual(tag.kind, KIND_CATEGORY)
