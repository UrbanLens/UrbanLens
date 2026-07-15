"""Tests for the "Auto-tagging" checkbox visibility in the label edit dialog.

Regression coverage for a bug where the checkbox was gated only on the
site-wide AI feature flag (``can_use_ai_features``), ignoring the user's own
AI settings entirely - so a user who had turned off "Enable AI Features", or
the specific per-kind toggle (Auto-tag/categorize/status pins), still saw an
"Auto-tagging" option in every label edit dialog that did nothing useful for
them, since ``Label.objects.eligible_for_auto_tag()``-style lookups (see
services/auto_tag.py) already exclude labels for a profile with these
settings off. The dialog should match reality.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label

_AI_ENABLED_PATCH = "urbanlens.dashboard.controllers.labels.user_has_feature"


class LabelEditAutoTagToggleVisibilityTests(TestCase):
    """AI-path visibility. Keyword-tagging fields default to True (see model.py), so every
    test here that expects the toggle to be HIDDEN must also disable the keyword path -
    otherwise the (independently sufficient) keyword defaults would keep it visible.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.profile.keyword_tagging_enabled = False
        self.profile.save(update_fields=["keyword_tagging_enabled"])
        self.client.force_login(self.user)
        self.label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.id})

    def _get(self):
        with patch(_AI_ENABLED_PATCH, return_value=True):
            return self.client.get(self.url)

    def test_hidden_when_site_ai_feature_is_off(self) -> None:
        self.profile.ai_enabled = True
        self.profile.ai_label_tags = True
        self.profile.save(update_fields=["ai_enabled", "ai_label_tags"])
        with patch(_AI_ENABLED_PATCH, return_value=False):
            response = self.client.get(self.url)
        self.assertNotContains(response, "allow_auto_tag")

    def test_hidden_when_users_master_ai_toggle_is_off(self) -> None:
        self.profile.ai_enabled = False
        self.profile.ai_label_tags = True
        self.profile.save(update_fields=["ai_enabled", "ai_label_tags"])
        response = self._get()
        self.assertNotContains(response, "allow_auto_tag")

    def test_hidden_when_users_per_kind_toggle_is_off(self) -> None:
        """ai_label_tags/categories/statuses default to False - the common case."""
        self.profile.ai_enabled = True
        self.profile.ai_label_tags = False
        self.profile.save(update_fields=["ai_enabled", "ai_label_tags"])
        response = self._get()
        self.assertNotContains(response, "allow_auto_tag")

    def test_shown_when_master_and_per_kind_toggles_are_both_on(self) -> None:
        self.profile.ai_enabled = True
        self.profile.ai_label_tags = True
        self.profile.save(update_fields=["ai_enabled", "ai_label_tags"])
        response = self._get()
        self.assertContains(response, "allow_auto_tag")

    def test_category_kind_checks_ai_label_categories_not_ai_label_tags(self) -> None:
        category = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="Factories")
        url = reverse("label.edit", kwargs={"label_kind": "category", "label_id": category.id})
        self.profile.ai_enabled = True
        self.profile.ai_label_tags = True
        self.profile.ai_label_categories = False
        self.profile.save(update_fields=["ai_enabled", "ai_label_tags", "ai_label_categories"])
        with patch(_AI_ENABLED_PATCH, return_value=True):
            response = self.client.get(url)
        self.assertNotContains(response, "allow_auto_tag")

    def test_status_kind_checks_ai_label_statuses(self) -> None:
        status = baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Abandoned")
        url = reverse("label.edit", kwargs={"label_kind": "status", "label_id": status.id})
        self.profile.ai_enabled = True
        self.profile.ai_label_statuses = True
        self.profile.save(update_fields=["ai_enabled", "ai_label_statuses"])
        with patch(_AI_ENABLED_PATCH, return_value=True):
            response = self.client.get(url)
        self.assertContains(response, "allow_auto_tag")


class LabelEditKeywordAutoTagToggleVisibilityTests(TestCase):
    """Keyword-path visibility - independent of AI settings entirely."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        # AI path fully off, so any visibility below is attributable to the keyword path.
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["ai_enabled"])
        self.client.force_login(self.user)
        self.label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.id})

    def _get(self):
        with patch(_AI_ENABLED_PATCH, return_value=False):
            return self.client.get(self.url)

    def test_shown_by_default(self) -> None:
        """keyword_tagging_enabled/keyword_label_tags both default to True."""
        response = self._get()
        self.assertContains(response, "allow_auto_tag")

    def test_hidden_when_users_master_keyword_toggle_is_off(self) -> None:
        self.profile.keyword_tagging_enabled = False
        self.profile.save(update_fields=["keyword_tagging_enabled"])
        response = self._get()
        self.assertNotContains(response, "allow_auto_tag")

    def test_hidden_when_users_per_kind_keyword_toggle_is_off(self) -> None:
        self.profile.keyword_label_tags = False
        self.profile.save(update_fields=["keyword_label_tags"])
        response = self._get()
        self.assertNotContains(response, "allow_auto_tag")

    def test_shown_when_ai_disabled_but_keyword_enabled(self) -> None:
        self.profile.ai_enabled = False
        self.profile.keyword_tagging_enabled = True
        self.profile.keyword_label_tags = True
        self.profile.save(update_fields=["ai_enabled", "keyword_tagging_enabled", "keyword_label_tags"])
        response = self._get()
        self.assertContains(response, "allow_auto_tag")

    def test_category_kind_checks_keyword_label_categories(self) -> None:
        category = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="Factories")
        url = reverse("label.edit", kwargs={"label_kind": "category", "label_id": category.id})
        self.profile.keyword_label_categories = False
        self.profile.save(update_fields=["keyword_label_categories"])
        with patch(_AI_ENABLED_PATCH, return_value=False):
            response = self.client.get(url)
        self.assertNotContains(response, "allow_auto_tag")

    def test_status_kind_checks_keyword_label_statuses(self) -> None:
        status = baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Abandoned")
        url = reverse("label.edit", kwargs={"label_kind": "status", "label_id": status.id})
        with patch(_AI_ENABLED_PATCH, return_value=False):
            response = self.client.get(url)
        self.assertContains(response, "allow_auto_tag")
