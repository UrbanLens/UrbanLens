"""Tests for the Stripe webhook receiver's replay protection.

``invoice.payment_succeeded`` is the one handler with a non-idempotent side
effect: ``banking.apply_payment`` *increments* ``total_paid_cents``, and that
figure drives the pay-what-you-want usage ledger (i.e. how long access stays
granted). So "was this event already handled" has to be answered against
committed state, not against a row read earlier in the same request.

Stripe redelivers on any non-2xx **or timeout**, which is what makes the
crash-after-side-effect case real rather than theoretical: if the payment is
credited but the delivery is not marked processed, the retry credits it again.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription, StripeWebhookEvent
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.UrbanLens.settings.app import settings as app_settings


def _stripe_subscription() -> dict:
    """The shape ``sync_from_stripe_subscription`` reads: status, plus the first item's price."""
    return {
        "id": "sub_test",
        "status": "active",
        "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": "price_test", "unit_amount": 1000}, "current_period_end": 1_800_000_000}]},
    }


def _event(event_id: str, subscription_id: str, amount_paid: int) -> dict:
    return {
        "id": event_id,
        "type": "invoice.payment_succeeded",
        "data": {"object": {"subscription": subscription_id, "amount_paid": amount_paid}},
    }


class StripeWebhookReplayTests(TestCase):
    """Every delivery of one event must credit the payment exactly once."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500)
        self.subscription = baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            stripe_subscription_id="sub_test",
            status=BillingSubscriptionStatus.ACTIVE,
            total_paid_cents=0,
        )
        self.url = reverse("billing.stripe_webhook")

    def _post(self, event: dict, client: Client | None = None):
        # The signature is verified before anything else runs, and the view 503s when no
        # signing secret is configured (which it isn't in a test env) - supply a secret and
        # stub the verification itself, rather than the view, so the real ordering
        # (verify -> record -> handle) still runs.
        with (
            mock.patch.object(app_settings, "stripe_webhook_secret", "whsec_test"),
            mock.patch("stripe.Webhook.construct_event", return_value=mock.Mock(to_dict=lambda: event)),
        ):
            return (client or self.client).post(self.url, data=json.dumps(event), content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=stub")

    @mock.patch("stripe.Subscription.retrieve")
    def test_a_redelivered_payment_is_credited_once(self, retrieve: mock.Mock) -> None:
        retrieve.return_value = mock.Mock(to_dict=_stripe_subscription)
        event = _event("evt_1", "sub_test", 1000)

        self.assertEqual(self._post(event).status_code, 200)
        self.assertEqual(self._post(event).status_code, 200)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 1000)

    @mock.patch("stripe.Subscription.retrieve")
    def test_a_delivery_that_fails_after_crediting_does_not_double_credit_on_retry(self, retrieve: mock.Mock) -> None:
        """The crash-after-side-effect case Stripe's retry policy guarantees will happen.

        If the credit commits but the "processed" marker does not, the redelivery sees
        an unprocessed event and credits a second time. Processing and the marker have
        to land in the same transaction for the retry to be a genuine no-op.
        """
        retrieve.return_value = mock.Mock(to_dict=_stripe_subscription)
        event = _event("evt_2", "sub_test", 1000)

        # First delivery: the payment is applied, then marking it processed blows up
        # (a DB blip, a killed worker, a lost response Stripe reads as a timeout).
        # Only that write fails - the initial insert of the audit row has to succeed, or
        # the retry would be handling a brand-new event and prove nothing.
        original_save = StripeWebhookEvent.save

        def fail_only_when_marking_processed(instance, *args, **kwargs):
            if "processed_at" in (kwargs.get("update_fields") or ()):
                raise RuntimeError("connection lost")
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(StripeWebhookEvent, "save", fail_only_when_marking_processed), self.assertRaises(RuntimeError):
            self._post(event)

        # Stripe retries the same event.
        self.assertEqual(self._post(event).status_code, 200)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 1000)
