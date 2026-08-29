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
from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

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

        # Same profile/kind, external APIs re-enabled - an "external_apis_enabled is ignored"
        # implementation would still pass above.
        self.profile.external_apis_enabled = True
        self.assertTrue(AutoTagService._ai_kind_enabled_for_profile(KIND_CATEGORY, self.profile))

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
    """suggest_for_pin must only invoke the stages the profile allows.

    The pin path's first stage is REData suggestions rather than keyword
    matching (a wiki, which has no owner and so no per-user taxonomy, still
    keyword-matches). The two stages are independently gated: REData on
    SiteFeature.AUTO_TAGGING plus the user's own switch, AI on SiteFeature.AI
    plus the per-kind AI preferences.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile: Profile = self.user.profile
        self.pin = _make_pin(self.profile)

    def _run(self, *, auto_tagging: bool):
        with (
            mock.patch.object(AutoTagService, "_redata_match", return_value=[]) as redata_match,
            mock.patch.object(AutoTagService, "_ai_match", return_value=[]) as ai_match,
            mock.patch.object(AutoTagService, "_eligible_labels", return_value=[baker.prepare(Label, kind=KIND_CATEGORY)]),
            mock.patch("urbanlens.dashboard.models.subscriptions.model.user_has_feature", return_value=auto_tagging),
        ):
            AutoTagService(kinds=[KIND_CATEGORY]).suggest_for_pin(self.pin)
        return redata_match, ai_match

    def test_the_capability_alone_runs_the_redata_stage(self) -> None:
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["ai_enabled"])

        redata_match, ai_match = self._run(auto_tagging=True)

        redata_match.assert_called_once()
        ai_match.assert_not_called()

    def test_the_user_switch_turns_the_redata_stage_off(self) -> None:
        self.profile.disable_auto_tagging = True
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["disable_auto_tagging", "ai_enabled"])

        redata_match, ai_match = self._run(auto_tagging=True)

        redata_match.assert_not_called()
        ai_match.assert_not_called()

    def test_neither_stage_runs_without_either_grant(self) -> None:
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["ai_enabled"])

        redata_match, ai_match = self._run(auto_tagging=False)

        redata_match.assert_not_called()
        ai_match.assert_not_called()

    def test_both_grants_run_both_stages(self) -> None:
        self.profile.ai_enabled = True
        self.profile.ai_label_categories = True
        self.profile.save(update_fields=["ai_enabled", "ai_label_categories"])

        redata_match, ai_match = self._run(auto_tagging=True)

        redata_match.assert_called_once()
        ai_match.assert_called_once()
