"""Regression coverage for the main map's saved-filter sidebar/toolbar UI bugs.

Covers two bugs reported against the filters sidebar's "Saved Filters"
section: clicking a chip merged in structured ``label_groups`` criteria
(from the map's own formula bar) but silently ignored the flat
``tags``/``exclude_tags`` shape a filter saved via the Filters tab's simple
include/exclude picker actually uses (that dialog has no formula-bar UI, so
every label-only filter created there stored *only* ``tags``/``exclude_tags``)
- for such a filter, clicking the chip merged nothing at all and looked like
a dead click. It also never gave the clicked chip any visual "applied" state.

Both fixes live in inline ``<script>`` markup inside
``dashboard/pages/map/index.html`` (``applySavedFilter()``), which has no
dedicated JS test runner in this project, so these tests instead assert
against the rendered page source - a lightweight guard against the specific
fix regressing, not a full behavioral test of the browser-side merge logic.

Also covers the ``#filter-form`` race: sliders, the debounced name/custom-
field inputs, and the saved-filters toolbar's manual ``htmx.trigger(form,
'change')`` calls are all independent triggers hitting the same
``hx-target="#map-body"`` with no ``hx-sync`` - out-of-order responses could
silently overwrite a just-applied toolbar filter's result with a stale one
from an earlier in-flight request, exactly matching the reported "clicking a
toolbar filter causes no changes" symptom when sidebar filters were already
active. ``hx-sync="this:replace"`` makes htmx cancel/replace the in-flight
request instead of racing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
