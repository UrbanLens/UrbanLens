"""Tests for services.billing.webhooks - handlers operate on plain dicts (the shape
produced by stripe.Event.to_dict()/stripe.Subscription.retrieve(...).to_dict()), not
live Stripe SDK objects. No real network access occurs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingCustomer, BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import webhooks


def _subscription_payload(
    *,
    sub_id: str = "sub_123",
    status: str = "active",
    price_id: str = "price_1",
    unit_amount: int = 500,
    item_id: str = "si_1",
    current_period_end: int = 1_700_000_000,
    cancel_at_period_end: bool = False,
    canceled_at: int | None = None,
) -> dict:
    return {
        "id": sub_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": canceled_at,
        "items": {
            "data": [
                {
                    "id": item_id,
                    "current_period_end": current_period_end,
                    "price": {"id": price_id, "unit_amount": unit_amount},
                }
            ]
        },
    }


class SyncFromStripeSubscriptionTests(TestCase):
    def test_copies_status_price_and_period_fields(self) -> None:
        subscription = baker.make(RoleSubscription, status=BillingSubscriptionStatus.INCOMPLETE, pledged_amount_cents=0)
        webhooks.sync_from_stripe_subscription(subscription, _subscription_payload(status="active", unit_amount=750, price_id="price_9"))

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.pledged_amount_cents, 750)
        self.assertEqual(subscription.stripe_price_id, "price_9")
        self.assertEqual(subscription.current_period_end, datetime.fromtimestamp(1_700_000_000, tz=UTC))

    def test_recomputes_threshold_met_for_dynamic_roles(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None)
        subscription = baker.make(RoleSubscription, role=role, threshold_met=True)
        with mock.patch("urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("10.00")):
            webhooks.sync_from_stripe_subscription(subscription, _subscription_payload(unit_amount=500))

        subscription.refresh_from_db()
        self.assertFalse(subscription.threshold_met)


class HandleCheckoutSessionCompletedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def _event(self, **session_overrides) -> dict:
        session = {
            "id": "evt_cs_1",
            "mode": "subscription",
            "subscription": "sub_123",
            "client_reference_id": str(self.user.pk),
            "customer": "cus_123",
            "metadata": {"role_id": str(self.role.pk)},
        }
        session.update(session_overrides)
        return {"type": "checkout.session.completed", "data": {"object": session}}

    def test_creates_billing_customer_and_role_subscription(self) -> None:
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload()
            webhooks.handle_event(self._event())

        self.assertTrue(BillingCustomer.objects.filter(user=self.user, stripe_customer_id="cus_123").exists())
        subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_123")
        self.assertEqual(subscription.user, self.user)
        self.assertEqual(subscription.role, self.role)
        self.assertEqual(subscription.status, "active")

    def test_ignores_non_subscription_mode_sessions(self) -> None:
        webhooks.handle_event(self._event(mode="payment"))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_missing_subscription_id_is_a_no_op(self) -> None:
        webhooks.handle_event(self._event(subscription=None))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_unknown_user_is_a_no_op(self) -> None:
        webhooks.handle_event(self._event(client_reference_id="999999"))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_unresolvable_role_is_a_no_op(self) -> None:
        webhooks.handle_event(self._event(metadata={"role_id": "999999"}))
        self.assertFalse(RoleSubscription.objects.exists())


class HandleSubscriptionUpdatedTests(TestCase):
    def test_syncs_an_existing_subscription(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.INCOMPLETE)
        event = {"type": "customer.subscription.updated", "data": {"object": _subscription_payload(status="active")}}
        webhooks.handle_event(event)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "active")

    def test_unknown_subscription_is_a_no_op(self) -> None:
        event = {"type": "customer.subscription.updated", "data": {"object": _subscription_payload(sub_id="sub_unknown")}}
        webhooks.handle_event(event)  # must not raise


class HandleSubscriptionDeletedTests(TestCase):
    def test_marks_the_subscription_canceled(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.ACTIVE)
        event = {"type": "customer.subscription.deleted", "data": {"object": _subscription_payload(canceled_at=1_700_000_000)}}
        webhooks.handle_event(event)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.CANCELED)
        self.assertEqual(subscription.canceled_at, datetime.fromtimestamp(1_700_000_000, tz=UTC))

    def test_unknown_subscription_is_a_no_op(self) -> None:
        event = {"type": "customer.subscription.deleted", "data": {"object": _subscription_payload(sub_id="sub_unknown")}}
        webhooks.handle_event(event)  # must not raise


class HandleInvoicePaymentSucceededTests(TestCase):
    def test_resyncs_from_a_freshly_retrieved_subscription(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", pledged_amount_cents=0)
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(unit_amount=900)
            webhooks.handle_event({"type": "invoice.payment_succeeded", "data": {"object": {"subscription": "sub_123"}}})

        subscription.refresh_from_db()
        self.assertEqual(subscription.pledged_amount_cents, 900)

    def test_no_subscription_on_invoice_is_a_no_op(self) -> None:
        webhooks.handle_event({"type": "invoice.payment_succeeded", "data": {"object": {}}})  # must not raise

    def test_unknown_subscription_is_a_no_op(self) -> None:
        webhooks.handle_event({"type": "invoice.payment_succeeded", "data": {"object": {"subscription": "sub_unknown"}}})  # must not raise


class HandleInvoicePaymentFailedTests(TestCase):
    def test_marks_the_subscription_past_due(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.ACTIVE)
        webhooks.handle_event({"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_123"}}})

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.PAST_DUE)

    def test_unknown_subscription_is_a_no_op(self) -> None:
        webhooks.handle_event({"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_unknown"}}})  # must not raise


class HandleEventUnknownTypeTests(TestCase):
    def test_unhandled_event_type_is_ignored(self) -> None:
        webhooks.handle_event({"type": "customer.updated", "data": {"object": {}}})  # must not raise
