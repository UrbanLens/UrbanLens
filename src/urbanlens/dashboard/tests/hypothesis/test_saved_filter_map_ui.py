"""Regression coverage for the main map's saved-filter sidebar/toolbar UI bugs."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_MAP_URL = "/dashboard/map/"


class SavedFilterMapUiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_filter_form_syncs_requests_to_avoid_out_of_order_swaps(self) -> None:
        resp = self.client.get(_MAP_URL)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('id="filter-form"', content)
        self.assertIn('hx-sync="this:replace"', content)

    def test_apply_saved_filter_merges_flat_tags_not_just_label_groups(self) -> None:
        resp = self.client.get(_MAP_URL)
        content = resp.content.decode()
        # The label_groups branch must stay - it's still the primary path for
        # filters saved from the map's own formula bar.
        self.assertIn("Array.isArray(criteria.label_groups)", content)
        # The new fallback branch for flat tags (Filters-tab-created filters).
        self.assertIn("Array.isArray(criteria.tags) && criteria.tags.length", content)

    def test_apply_saved_filter_marks_the_clicked_chip_as_active(self) -> None:
        resp = self.client.get(_MAP_URL)
        content = resp.content.decode()
        self.assertIn("fp-saved-filter-apply--active", content)
        # resetFilters() must clear it again so a fresh panel doesn't show a
        # stale "applied" chip from a previous session's filter state.
        self.assertIn("querySelectorAll('.fp-saved-filter-apply--active')", content)
