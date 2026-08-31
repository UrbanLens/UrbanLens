"""Regression coverage for UL-193's bulk-rating UI wiring.

The backend (PinBulkEditView) already supports a "rating" field in its
POST body - this checks the client-side pieces that actually let a user
trigger it: the <select> in the bulk-edit dialog, and the JS that reads
it into the request payload. This is inline client-side JS with no
browser available in this environment, so only markup presence can be
verified here, not runtime behavior.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


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
        body = self.client.get(reverse("map.view")).content.decode()
        # Anchor on the actual handler *registration*, not just any occurrence of the
        # button id (which also appears earlier in the dialog's markup) - a bare
        # "addEventListener('click'" search from the markup position would instead land
        # on the first unrelated click handler defined anywhere later on the page.
        handler_index = body.find("getElementById('bulk-edit-confirm-btn').addEventListener('click'")
        self.assertNotEqual(handler_index, -1, "confirm-btn click handler registration not found")
        # The route name itself ("pin.bulk_edit") never appears literally in rendered
        # output - only the resolved URL does - so search for that instead. Searching
        # for the unresolved name always missed (index -1), and slicing with a -1 end
        # silently included nearly the rest of the page rather than failing loudly,
        # so the two assertIn checks below were passing regardless of where in the
        # handler (or after it) the rating wiring actually lived.
        bulk_edit_url = reverse("pin.bulk_edit")
        payload_send_index = body.find(bulk_edit_url, handler_index)
        self.assertNotEqual(payload_send_index, -1, "bulk-edit POST call not found after the handler")
        handler_body = body[handler_index:payload_send_index]
        self.assertIn("bulk-edit-rating-value", handler_body)
        self.assertIn("payload.rating", handler_body)
        # The select's "No change" option submits '' - pin the guard that keeps that
        # value from being sent as the rating (e.g. as NaN, clobbering every selected
        # pin's rating) rather than just the mere presence of "payload.rating" text.
        self.assertIn("if (ratingValue !== '')", handler_body)
