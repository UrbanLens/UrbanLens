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
        self.assertIn('<option value="0">Clear rating</option>', body)
        self.assertIn('<option value="5">', body)

    def test_confirm_handler_reads_the_rating_select_into_the_payload(self) -> None:
        body = self.client.get(reverse("map.view")).content.decode()
        confirm_index = body.find("bulk-edit-confirm-btn")
        handler_index = body.find("addEventListener('click'", confirm_index)
        payload_send_index = body.find("pin.bulk_edit", handler_index)
        handler_body = body[handler_index:payload_send_index]
        self.assertIn("bulk-edit-rating-value", handler_body)
        self.assertIn("payload.rating", handler_body)
