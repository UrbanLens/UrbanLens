"""Auto-tagging: the matcher pipeline, and each stage's own gating."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.points import LABEL_MATCHERS
from urbanlens.dashboard.services.labels.auto_tag import AutoTagService
from urbanlens.dashboard.services.labels.matching import KeywordLabelMatcher, LabelMatcher

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _make_pin(profile: Profile) -> Pin:
    offset = next(_COORDS)
    location = baker.make(
        "dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}"
    )
    return baker.make(Pin, profile=profile, location=location)


class KeywordKindEnabledForProfileTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = _make_pin(self.profile)

    def test_enabled_by_default(self) -> None:
        """keyword_tagging_enabled and the per-kind fields default to True."""
        self.assertTrue(KeywordLabelMatcher().applies(self.pin, KIND_CATEGORY, self.profile))
        self.assertTrue(KeywordLabelMatcher().applies(self.pin, KIND_TAG, self.profile))
        self.assertTrue(KeywordLabelMatcher().applies(self.pin, KIND_STATUS, self.profile))

    def test_disabled_when_master_keyword_toggle_off(self) -> None:
        self.profile.keyword_tagging_enabled = False
        self.assertFalse(KeywordLabelMatcher().applies(self.pin, KIND_CATEGORY, self.profile))

    def test_disabled_when_per_kind_flag_off(self) -> None:
        self.profile.keyword_label_tags = False
        self.assertFalse(KeywordLabelMatcher().applies(self.pin, KIND_TAG, self.profile))

    def test_ignores_external_apis_disabled(self) -> None:
        """Keyword matching makes no API call, so it isn't gated on external_apis_enabled."""
        self.profile.external_apis_enabled = False
        self.assertTrue(KeywordLabelMatcher().applies(self.pin, KIND_CATEGORY, self.profile))


class _Fixed(LabelMatcher):
    """A stage that chooses given labels, recording what it was offered."""

    def __init__(self, slug: str, chosen: list[Label], *, applies: bool = True, fails: bool = False) -> None:
        self.slug = slug
        self.chosen = chosen
        self._applies = applies
        self.fails = fails
        self.offered: list[list[Label]] = []

    def applies(self, target, kind, profile) -> bool:
        return self._applies

    def match(self, target, eligible, kind, *, limit):
        self.offered.append(list(eligible))
        if self.fails:
            raise RuntimeError("unavailable")
        return [label for label in self.chosen if label in eligible][:limit]


class PipelineTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = _make_pin(self.profile)
        self.labels = [baker.make(Label, kind=KIND_CATEGORY, name=f"label-{index}", profile=None) for index in range(4)]

    def test_each_stage_is_offered_only_what_earlier_stages_left(self) -> None:
        first = _Fixed("first", self.labels[:1])
        second = _Fixed("second", self.labels[:2])
        with LABEL_MATCHERS.overridden([first, second]):
            matched = AutoTagService().match(self.pin, self.labels, KIND_CATEGORY, self.profile)
        self.assertEqual(matched, self.labels[:2])
        self.assertEqual(second.offered, [self.labels[1:]])

    def test_the_limit_stops_the_pipeline(self) -> None:
        first = _Fixed("first", self.labels[:2])
        second = _Fixed("second", self.labels[2:])
        with LABEL_MATCHERS.overridden([first, second]):
            matched = AutoTagService(max_labels=2).match(self.pin, self.labels, KIND_CATEGORY, self.profile)
        self.assertEqual(matched, self.labels[:2])
        self.assertEqual(second.offered, [])

    def test_a_failing_or_inapplicable_stage_is_passed_over(self) -> None:
        broken = _Fixed("broken", self.labels, fails=True)
        off = _Fixed("off", self.labels, applies=False)
        working = _Fixed("working", self.labels[3:])
        with (
            LABEL_MATCHERS.overridden([broken, off, working]),
            self.assertLogs("urbanlens.dashboard.services.labels.auto_tag", level="ERROR"),
        ):
            matched = AutoTagService().match(self.pin, self.labels, KIND_CATEGORY, self.profile)
        self.assertEqual(matched, self.labels[3:])
        self.assertEqual(off.offered, [])

    def test_a_stage_cannot_choose_a_label_it_was_not_offered(self) -> None:
        foreign = baker.make(Label, kind=KIND_CATEGORY, name="not-eligible", profile=None)
        rogue = _Fixed("rogue", [])
        rogue.match = lambda target, eligible, kind, *, limit: [foreign, self.labels[0]]
        with LABEL_MATCHERS.overridden([rogue]):
            self.assertEqual(
                AutoTagService().match(self.pin, self.labels, KIND_CATEGORY, self.profile), self.labels[:1]
            )

    def test_pins_are_keyword_matched(self) -> None:
        church = baker.make(Label, kind=KIND_CATEGORY, name="Church", profile=None, allow_auto_tag=True)
        with (
            mock.patch.object(KeywordLabelMatcher, "keyword_text", return_value="St. Mary's Church"),
            LABEL_MATCHERS.overridden([KeywordLabelMatcher()]),
        ):
            self.assertIn(church, AutoTagService().suggest_for_pin(self.pin))
