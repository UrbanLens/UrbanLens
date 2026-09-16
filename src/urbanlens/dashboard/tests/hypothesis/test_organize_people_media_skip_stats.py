"""People and media tabs render no pin-count stats, so computing them is waste.

`_organize_label_card.html` guards every stat span with
`{% if kind != 'people' and kind != 'media' %}` - these two kinds never display a
pin count or a total-pin count. Found by adversarial review of the X25 fix, which
moved every label tab onto `.with_pin_counts()` plus `prime_total_pin_counts()`
without checking that two of the five tabs never read either result: two
correlated subqueries and a discarded per-child `Count()` per label, plus two
more queries to prime a subtree total nothing displays.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.organize import build_organize_page_context
from urbanlens.dashboard.models.labels.meta import KIND_MEDIA, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class PeopleAndMediaTabsSkipStatsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def _context(self, tab: str) -> dict:
        request = RequestFactory().get("/organize/", {"tab": tab})
        request.user = self.user
        return build_organize_page_context(request, tab)

    def test_people_labels_are_not_primed_for_total_pin_count(self) -> None:
        # `_total_pins_memo` defaults to None on every instance (model.py:93), so the
        # signal that priming ran is the value, not attribute presence.
        baker.make(Label, profile=self.profile, kind=KIND_USER, name="Alex")
        label = self._context("people")["user_labels"][0]
        self.assertIsNone(label._total_pins_memo, "primed a stat the template never reads")  # noqa: SLF001

    def test_media_labels_are_not_primed_for_total_pin_count(self) -> None:
        baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Photo")
        label = self._context("media")["media_labels"][0]
        self.assertIsNone(label._total_pins_memo, "primed a stat the template never reads")  # noqa: SLF001

    def test_people_labels_carry_no_pin_count_annotation(self) -> None:
        baker.make(Label, profile=self.profile, kind=KIND_USER, name="Alex")
        label = self._context("people")["user_labels"][0]
        self.assertFalse(hasattr(label, "pin_count"), "annotated a stat the template never reads")

    def test_media_labels_carry_no_pin_count_annotation(self) -> None:
        baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Photo")
        label = self._context("media")["media_labels"][0]
        self.assertFalse(hasattr(label, "pin_count"), "annotated a stat the template never reads")

    def test_a_stats_tab_still_gets_its_stats(self) -> None:
        """The negative half: this must not regress into no tab getting stats."""
        baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        label = self._context("tags")["tags"][0]
        self.assertIsNotNone(label._total_pins_memo)  # noqa: SLF001
        self.assertTrue(hasattr(label, "pin_count"))
