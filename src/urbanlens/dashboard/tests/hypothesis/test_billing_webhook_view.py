"""Tests for controllers.billing_webhooks.StripeWebhookView - signature verification,
idempotency, and dispatch. stripe.Webhook.construct_event is mocked; no real network
access or signature computation occurs."""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
import stripe

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import StripeWebhookEvent
from urbanlens.UrbanLens.settings.app import settings as app_settings

_EVENT = {"id": "evt_123", "type": "checkout.session.completed", "data": {"object": {"mode": "subscription"}}}


def _mock_event(payload: dict = _EVENT):
    event = mock.MagicMock()
    event.to_dict.return_value = payload
    return event


class StripeWebhookViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._secret_patch = mock.patch.object(app_settings, "stripe_webhook_secret", "whsec_test")
        self._secret_patch.start()
        self.addCleanup(self._secret_patch.stop)

    def test_valid_signature_processes_the_event_and_marks_it_processed(self) -> None:
        with (
            mock.patch("stripe.Webhook.construct_event", return_value=_mock_event()),
            mock.patch("urbanlens.dashboard.services.billing.webhooks.handle_event") as mock_handle,
        ):
            response = self.client.post(reverse("billing.stripe_webhook"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig")

        self.assertEqual(response.status_code, 200)
        mock_handle.assert_called_once_with(_EVENT)
        webhook_event = StripeWebhookEvent.objects.get(stripe_event_id="evt_123")
        self.assertIsNotNone(webhook_event.processed_at)
        self.assertEqual(webhook_event.event_type, "checkout.session.completed")

    def test_invalid_signature_is_rejected_and_nothing_is_stored(self) -> None:
        with mock.patch("stripe.Webhook.construct_event", side_effect=stripe.SignatureVerificationError("bad sig", "sig_header")):
            response = self.client.post(reverse("billing.stripe_webhook"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="bad")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StripeWebhookEvent.objects.exists())

    def test_missing_webhook_secret_returns_503(self) -> None:
        with mock.patch.object(app_settings, "stripe_webhook_secret", None):
            response = self.client.post(reverse("billing.stripe_webhook"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig")

        self.assertEqual(response.status_code, 503)

    def test_replaying_an_already_processed_event_does_not_reinvoke_the_handler(self) -> None:
        with (
            mock.patch("stripe.Webhook.construct_event", return_value=_mock_event()),
            mock.patch("urbanlens.dashboard.services.billing.webhooks.handle_event") as mock_handle,
        ):
            first = self.client.post(reverse("billing.stripe_webhook"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig")
            second = self.client.post(reverse("billing.stripe_webhook"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mock_handle.assert_called_once()
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_123").count(), 1)
