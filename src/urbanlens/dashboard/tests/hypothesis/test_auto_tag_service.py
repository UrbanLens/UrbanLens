"""Tests for AutoTagService's independent keyword-based and AI-based gating.

Regression coverage for the keyword-based auto-tagging user setting: previously
keyword matching (local pattern/substring matching, no external API call) and AI
matching (LLM call) shared a single set of profile gates (ai_enabled/ai_label_*),
so a user could not disable the free keyword path independently of the paid AI
path, or vice versa. Profile gained keyword_tagging_enabled/keyword_label_tags/
keyword_label_categories/keyword_label_statuses (default True, since keyword
matching is free and local) alongside the existing ai_* fields (default False).
"""

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
from urbanlens.dashboard.services.auto_tag import AutoTagService

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _make_pin(profile: Profile) -> Pin:
    offset = next(_COORDS)
    location = baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}")
    return baker.make(Pin, profile=profile, location=location)


class AiKindEnabledForProfileTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_disabled_when_master_ai_toggle_off(self) -> None:
        self.profile.ai_enabled = False
        self.profile.ai_label_categories = True
        self.assertFalse(AutoTagService._ai_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

    def test_disabled_when_external_apis_disabled(self) -> None:
        self.profile.ai_enabled = True
        self.profile.external_apis_enabled = False
        self.profile.ai_label_categories = True
        self.assertFalse(AutoTagService._ai_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

    def test_disabled_when_per_kind_flag_off(self) -> None:
        self.profile.ai_enabled = True
        self.profile.ai_label_categories = False
        self.assertFalse(AutoTagService._ai_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

    def test_enabled_when_all_flags_on(self) -> None:
        self.profile.ai_enabled = True
        self.profile.ai_label_categories = True
        self.assertTrue(AutoTagService._ai_kind_enabled_for_profile(KIND_CATEGORY, self.profile))


class KeywordKindEnabledForProfileTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_enabled_by_default(self) -> None:
        """keyword_tagging_enabled and the per-kind fields default to True."""
        self.assertTrue(AutoTagService._keyword_kind_enabled_for_profile(KIND_CATEGORY, self.profile))
        self.assertTrue(AutoTagService._keyword_kind_enabled_for_profile(KIND_TAG, self.profile))
        self.assertTrue(AutoTagService._keyword_kind_enabled_for_profile(KIND_STATUS, self.profile))

    def test_disabled_when_master_keyword_toggle_off(self) -> None:
        self.profile.keyword_tagging_enabled = False
        self.assertFalse(AutoTagService._keyword_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

    def test_disabled_when_per_kind_flag_off(self) -> None:
        self.profile.keyword_label_tags = False
        self.assertFalse(AutoTagService._keyword_kind_enabled_for_profile(KIND_TAG, self.profile))

    def test_ignores_external_apis_disabled(self) -> None:
        """Keyword matching makes no API call, so it isn't gated on external_apis_enabled."""
        self.profile.external_apis_enabled = False
        self.assertTrue(AutoTagService._keyword_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

    def test_ignores_ai_enabled(self) -> None:
        """Keyword matching is independent of the AI master toggle."""
        self.profile.ai_enabled = False
        self.assertTrue(AutoTagService._keyword_kind_enabled_for_profile(KIND_CATEGORY, self.profile))


class SuggestForPinStageGatingTests(TestCase):
    """suggest_for_pin must only invoke the keyword/AI matching stages the profile allows."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile: Profile = self.user.profile
        self.pin = _make_pin(self.profile)

    def _run(self):
        with (
            mock.patch.object(AutoTagService, "_keyword_match", return_value=[]) as keyword_match,
            mock.patch.object(AutoTagService, "_ai_match", return_value=[]) as ai_match,
            mock.patch.object(AutoTagService, "_eligible_labels", return_value=[baker.prepare(Label, kind=KIND_CATEGORY)]),
        ):
            AutoTagService(kinds=[KIND_CATEGORY]).suggest_for_pin(self.pin)
        return keyword_match, ai_match

    def test_keyword_only_runs_keyword_stage_not_ai(self) -> None:
        self.profile.keyword_tagging_enabled = True
        self.profile.keyword_label_categories = True
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["keyword_tagging_enabled", "keyword_label_categories", "ai_enabled"])
        keyword_match, ai_match = self._run()
        keyword_match.assert_called_once()
        ai_match.assert_not_called()

    def test_ai_only_runs_ai_stage_not_keyword(self) -> None:
        self.profile.keyword_tagging_enabled = False
        self.profile.ai_enabled = True
        self.profile.ai_label_categories = True
        self.profile.save(update_fields=["keyword_tagging_enabled", "ai_enabled", "ai_label_categories"])
        keyword_match, ai_match = self._run()
        keyword_match.assert_not_called()
        ai_match.assert_called_once()

    def test_both_disabled_runs_neither_stage(self) -> None:
        self.profile.keyword_tagging_enabled = False
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["keyword_tagging_enabled", "ai_enabled"])
        keyword_match, ai_match = self._run()
        keyword_match.assert_not_called()
        ai_match.assert_not_called()

    def test_both_enabled_runs_both_stages(self) -> None:
        self.profile.keyword_tagging_enabled = True
        self.profile.keyword_label_categories = True
        self.profile.ai_enabled = True
        self.profile.ai_label_categories = True
        self.profile.save(update_fields=["keyword_tagging_enabled", "keyword_label_categories", "ai_enabled", "ai_label_categories"])
        keyword_match, ai_match = self._run()
        keyword_match.assert_called_once()
        ai_match.assert_called_once()
