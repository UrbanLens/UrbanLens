"""The Organize page renders a label's picker entry once, not once per hidden picker.

Nine parent/child pickers sit in the page's hidden create and bulk-edit dialogs, and each rendered every label the
account can see, twice, as two templates apiece: 1,704 template renders and ~220 ms of CPU for an account with 55
labels, on every visit, whichever tab was open.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label

MORE_LABELS = 10
#: A tag on the open tab is a card, plus one picker entry (a button and its chip) shared by every picker.
RENDERS_PER_LABEL = 4


class OrganizePageRendersEachLabelOnceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.made = 0

    def _tags(self, count: int) -> list[Label]:
        made = []
        for _ in range(count):
            self.made += 1
            made.append(Label.objects.create(name=f"Tag {self.made}", kind=KIND_TAG, profile=self.profile))
        return made

    def _page(self):
        response = self.client.get(reverse("organize.index"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_template_renders_do_not_grow_with_every_picker(self) -> None:
        self._tags(3)
        baseline = len(self._page().templates)

        self._tags(MORE_LABELS)
        grown = len(self._page().templates) - baseline

        self.assertLessEqual(
            grown, MORE_LABELS * RENDERS_PER_LABEL, f"{MORE_LABELS} more tags cost {grown} more renders"
        )

    def test_every_label_is_still_offered_as_a_parent_or_child(self) -> None:
        tags = self._tags(3)

        content = self._page().content.decode()

        for tag in tags:
            self.assertIn(f'data-id="{tag.id}"', content)
