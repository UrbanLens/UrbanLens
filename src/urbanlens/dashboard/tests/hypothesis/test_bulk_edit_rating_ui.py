"""Regression coverage for UL-193's bulk-rating UI wiring."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

#: P92 moved this from an inline <script> into a bundled TS entry (P124) - the handler
#: wiring is static code, so it is checked against the source file, not a response body.
_SCRIPT_SOURCE = (Path(__file__).resolve().parents[3] / "dashboard/frontend/ts/entries/map-page.ts").read_text()


class BulkEditRatingUiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_bulk_edit_dialog_has_a_rating_select(self) -> None:
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn('id="bulk-edit-rating-value"', body)
        self.assertIn('<option value="">No change</option>', body)
        self.assertIn('<option value="0">Clear rating</option>', body)
        self.assertIn('<option value="5">', body)

    def test_confirm_handler_reads_the_rating_select_into_the_payload(self) -> None:
        # Anchor on the actual handler *registration*, not just any occurrence of the
        # button id (which also appears earlier in the dialog's markup) - a bare
        # "addEventListener("click"" search from the markup position would instead land
        # on the first unrelated click handler defined anywhere later on the page.
        handler_index = _SCRIPT_SOURCE.find(
            'document.getElementById("bulk-edit-confirm-btn")!.addEventListener("click"'
        )
        self.assertNotEqual(handler_index, -1, "confirm-btn click handler registration not found")
        # The route name itself ("pin.bulk_edit") never appears literally in the script - only
        # the resolved URL, read off the page's own config at runtime, does - so search for the
        # config property the handler reads instead.
        payload_send_index = _SCRIPT_SOURCE.find("MAP_CFG.urls.pinBulkEdit", handler_index)
        self.assertNotEqual(payload_send_index, -1, "bulk-edit POST call not found after the handler")
        handler_body = _SCRIPT_SOURCE[handler_index:payload_send_index]
        self.assertIn("bulk-edit-rating-value", handler_body)
        self.assertIn("payload.rating", handler_body)
        # The select's "No change" option submits '' - pin the guard that keeps that
        # value from being sent as the rating (e.g. as NaN, clobbering every selected
        # pin's rating) rather than just the mere presence of "payload.rating" text.
        self.assertIn('if (ratingValue !== "")', handler_body)
