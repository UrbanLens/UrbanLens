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
    metadata: dict | None = None,
) -> dict:
    return {
        "id": sub_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": canceled_at,
        "metadata": metadata or {},
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
        webhooks.sync_from_stripe_subscription(
            subscription,
            _subscription_payload(
                status="active",
                unit_amount=750,
                price_id="price_9",
                cancel_at_period_end=True,
                canceled_at=1_650_000_000,
            ),
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.pledged_amount_cents, 750)
        self.assertEqual(subscription.stripe_price_id, "price_9")
        self.assertEqual(subscription.current_period_end, datetime.fromtimestamp(1_700_000_000, tz=UTC))
        self.assertTrue(subscription.cancel_at_period_end)
        self.assertEqual(subscription.canceled_at, datetime.fromtimestamp(1_650_000_000, tz=UTC))

    def test_recomputes_threshold_met_for_dynamic_roles(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        subscription = baker.make(RoleSubscription, role=role, threshold_met=True)
        with mock.patch(
            "urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("10.00")
        ):
            webhooks.sync_from_stripe_subscription(subscription, _subscription_payload(unit_amount=500))

        subscription.refresh_from_db()
        self.assertFalse(subscription.threshold_met)

    def test_recompute_threshold_met_marks_a_cleared_pledge_true(self) -> None:
        """Complement of the test above: a mutation that always resolves the recompute
        as unmet (or skips it) would still pass a suite that only ever exercises the
        below-threshold direction.
        """
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        subscription = baker.make(RoleSubscription, role=role, threshold_met=False)
        with mock.patch(
            "urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("10.00")
        ):
            webhooks.sync_from_stripe_subscription(subscription, _subscription_payload(unit_amount=1500))

        subscription.refresh_from_db()
        self.assertTrue(subscription.threshold_met)


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

    def test_missing_client_reference_id_is_a_no_op(self) -> None:
        """The other half of the ``not subscription_id or not user_id`` guard - only the
        subscription_id side was previously exercised.
        """
        webhooks.handle_event(self._event(client_reference_id=None))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_missing_customer_id_does_not_create_a_billing_customer(self) -> None:
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload()
            webhooks.handle_event(self._event(customer=None))

        self.assertFalse(BillingCustomer.objects.exists())
        self.assertTrue(RoleSubscription.objects.filter(stripe_subscription_id="sub_123").exists())

    def test_unknown_user_is_a_no_op(self) -> None:
        webhooks.handle_event(self._event(client_reference_id="999999"))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_unresolvable_role_is_a_no_op(self) -> None:
        webhooks.handle_event(self._event(metadata={"role_id": "999999"}))
        self.assertFalse(RoleSubscription.objects.exists())

    def test_carries_forward_the_usage_ledger_from_a_prior_canceled_row(self) -> None:
        pwyw_role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        from django.utils import timezone

        covered_until = timezone.now() + timezone.timedelta(days=45)
        baker.make(
            RoleSubscription,
            user=self.user,
            role=pwyw_role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=3000,
            amount_used_cents=1500,
            usage_covered_until=covered_until,
        )

        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(sub_id="sub_new")
            webhooks.handle_event(self._event(subscription="sub_new", metadata={"role_id": str(pwyw_role.pk)}))

        new_subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_new")
        self.assertEqual(new_subscription.total_paid_cents, 3000)
        self.assertEqual(new_subscription.amount_used_cents, 1500)
        self.assertEqual(new_subscription.usage_covered_until, covered_until)

    def test_fresh_pwyw_subscription_with_no_prior_row_starts_the_ledger_at_zero(self) -> None:
        pwyw_role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload()
            webhooks.handle_event(self._event(metadata={"role_id": str(pwyw_role.pk)}))

        subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_123")
        self.assertEqual(subscription.total_paid_cents, 0)
        self.assertEqual(subscription.amount_used_cents, 0)
        self.assertIsNone(subscription.usage_covered_until)

    def test_non_pwyw_role_does_not_carry_forward_a_prior_ledger(self) -> None:
        """The carry-forward branch is gated on role.pay_what_you_want - a fixed-price
        role must start fresh at zero even with a prior canceled row sitting around with
        a nonzero ledger, since that ledger has no meaning for a non-PWYW role.
        """
        baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=3000,
            amount_used_cents=1500,
        )

        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload()
            webhooks.handle_event(self._event())

        subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_123")
        self.assertEqual(subscription.total_paid_cents, 0)
        self.assertEqual(subscription.amount_used_cents, 0)

    def test_carries_forward_the_usage_ledger_from_the_most_recent_prior_row(self) -> None:
        """Two canceled rows exist for this (user, role) - the newer one's ledger must
        win, not just "a" previous row, which pins the order_by("-created") direction.
        """
        from django.utils import timezone

        pwyw_role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        older = baker.make(
            RoleSubscription,
            user=self.user,
            role=pwyw_role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=1000,
            amount_used_cents=500,
        )
        RoleSubscription.objects.filter(pk=older.pk).update(created=timezone.now() - timezone.timedelta(days=10))
        newer = baker.make(
            RoleSubscription,
            user=self.user,
            role=pwyw_role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=9000,
            amount_used_cents=4500,
        )
        RoleSubscription.objects.filter(pk=newer.pk).update(created=timezone.now() - timezone.timedelta(days=1))

        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(sub_id="sub_new")
            webhooks.handle_event(self._event(subscription="sub_new", metadata={"role_id": str(pwyw_role.pk)}))

        new_subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_new")
        self.assertEqual(new_subscription.total_paid_cents, 9000)
        self.assertEqual(new_subscription.amount_used_cents, 4500)


class HandleSubscriptionUpdatedTests(TestCase):
    def test_syncs_an_existing_subscription(self) -> None:
        subscription = baker.make(
            RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.INCOMPLETE
        )
        event = {"type": "customer.subscription.updated", "data": {"object": _subscription_payload(status="active")}}
        webhooks.handle_event(event)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "active")

    def test_unknown_subscription_is_a_no_op(self) -> None:
        event = {
            "type": "customer.subscription.updated",
            "data": {"object": _subscription_payload(sub_id="sub_unknown")},
        }
        webhooks.handle_event(event)  # must not raise

    def test_a_late_update_cannot_resurrect_a_canceled_subscription(self) -> None:
        """Stripe guarantees neither ordering nor single delivery.

        ``customer.subscription.updated`` and ``.deleted`` are emitted together
        at cancellation, and Stripe retries a failed delivery with backoff for
        days - so the ``updated`` carrying the pre-cancellation status can land
        after the ``deleted``. Applying it verbatim hands the subscription back
        its old status, and with it whatever access the role grants. The daily
        reconciliation sweep would undo that, but not for up to 24 hours.

        Cancellation is terminal at Stripe - a canceled subscription is never
        reactivated, a new one is created instead - so a later payload claiming
        otherwise is always the stale one.
        """
        subscription = baker.make(
            RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.CANCELED
        )

        webhooks.handle_event(
            {"type": "customer.subscription.updated", "data": {"object": _subscription_payload(status="active")}}
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.CANCELED)

    def test_a_canceled_payload_still_applies_to_a_canceled_subscription(self) -> None:
        """The guard must not block ordinary re-delivery of the cancellation itself."""
        subscription = baker.make(
            RoleSubscription,
            stripe_subscription_id="sub_123",
            status=BillingSubscriptionStatus.CANCELED,
            pledged_amount_cents=100,
        )

        webhooks.handle_event(
            {
                "type": "customer.subscription.updated",
                "data": {"object": _subscription_payload(status="canceled", unit_amount=500)},
            }
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.CANCELED)
        self.assertEqual(subscription.pledged_amount_cents, 500)


class HandleSubscriptionDeletedTests(TestCase):
    def test_marks_the_subscription_canceled(self) -> None:
        subscription = baker.make(
            RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.ACTIVE
        )
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": _subscription_payload(canceled_at=1_700_000_000)},
        }
        webhooks.handle_event(event)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.CANCELED)
        self.assertEqual(subscription.canceled_at, datetime.fromtimestamp(1_700_000_000, tz=UTC))

    def test_unknown_subscription_is_a_no_op(self) -> None:
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": _subscription_payload(sub_id="sub_unknown")},
        }
        webhooks.handle_event(event)  # must not raise

    def test_missing_canceled_at_falls_back_to_now(self) -> None:
        from django.utils import timezone

        subscription = baker.make(
            RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.ACTIVE
        )
        before = timezone.now()
        event = {"type": "customer.subscription.deleted", "data": {"object": _subscription_payload(canceled_at=None)}}
        webhooks.handle_event(event)
        after = timezone.now()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.CANCELED)
        self.assertIsNotNone(subscription.canceled_at)
        self.assertGreaterEqual(subscription.canceled_at, before)
        self.assertLessEqual(subscription.canceled_at, after)


class HandleInvoicePaymentSucceededTests(TestCase):
    def test_resyncs_from_a_freshly_retrieved_subscription(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", pledged_amount_cents=0)
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(unit_amount=900)
            webhooks.handle_event(
                {"type": "invoice.payment_succeeded", "data": {"object": {"subscription": "sub_123"}}}
            )

        subscription.refresh_from_db()
        self.assertEqual(subscription.pledged_amount_cents, 900)

    def test_banks_usage_ledger_for_a_pwyw_role(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        subscription = baker.make(
            RoleSubscription, role=role, stripe_subscription_id="sub_123", pledged_amount_cents=1000
        )
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(unit_amount=1000)
            webhooks.handle_event(
                {
                    "type": "invoice.payment_succeeded",
                    "data": {"object": {"subscription": "sub_123", "amount_paid": 1000}},
                }
            )

        subscription.refresh_from_db()
        self.assertEqual(subscription.total_paid_cents, 1000)
        self.assertTrue(subscription.has_banked_access)

    def test_does_not_bank_usage_ledger_for_a_fixed_price_role(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=500)
        subscription = baker.make(
            RoleSubscription, role=role, stripe_subscription_id="sub_123", pledged_amount_cents=500
        )
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(unit_amount=500)
            webhooks.handle_event(
                {
                    "type": "invoice.payment_succeeded",
                    "data": {"object": {"subscription": "sub_123", "amount_paid": 500}},
                }
            )

        subscription.refresh_from_db()
        self.assertEqual(subscription.total_paid_cents, 0)

    def test_missing_amount_paid_does_not_raise(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        subscription = baker.make(
            RoleSubscription, role=role, stripe_subscription_id="sub_123", pledged_amount_cents=1000
        )
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(unit_amount=1000)
            webhooks.handle_event(
                {"type": "invoice.payment_succeeded", "data": {"object": {"subscription": "sub_123"}}}
            )

        subscription.refresh_from_db()
        self.assertEqual(subscription.total_paid_cents, 0)

    def test_no_subscription_on_invoice_is_a_no_op(self) -> None:
        webhooks.handle_event({"type": "invoice.payment_succeeded", "data": {"object": {}}})  # must not raise

    def test_out_of_order_delivery_still_banks_the_payment(self) -> None:
        """Regression: Stripe guarantees neither webhook ordering nor delivery order,
        so invoice.payment_succeeded can arrive before checkout.session.completed has
        created the RoleSubscription row. The old code just logged and returned in
        that case - the payment was never banked, and nothing ever retries a webhook
        Stripe already recorded as delivered, so the loss was permanent for a
        pay-what-you-want role. The Subscription's own metadata (duplicated there at
        checkout time specifically for this - see stripe_client.create_checkout_session)
        must be used to create the row on the spot instead of giving up.
        """
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        user = baker.make(User)
        self.assertFalse(RoleSubscription.objects.filter(stripe_subscription_id="sub_new").exists())

        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(
                sub_id="sub_new", unit_amount=1000, metadata={"user_id": str(user.pk), "role_id": str(role.pk)}
            )
            webhooks.handle_event(
                {
                    "type": "invoice.payment_succeeded",
                    "data": {"object": {"subscription": "sub_new", "amount_paid": 1000}},
                }
            )

        subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_new")
        self.assertEqual(subscription.user, user)
        self.assertEqual(subscription.role, role)
        self.assertEqual(subscription.total_paid_cents, 1000)

    def test_out_of_order_delivery_carries_forward_a_prior_pwyw_ledger(self) -> None:
        """The recovery path must not bypass the same usage-ledger carry-forward a
        normal checkout.session.completed creation gets - otherwise resubscribing
        under this exact race would silently lose the prior banked balance."""
        from django.utils import timezone

        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        user = baker.make(User)
        covered_until = timezone.now() + timezone.timedelta(days=45)
        baker.make(
            RoleSubscription,
            user=user,
            role=role,
            status=BillingSubscriptionStatus.CANCELED,
            total_paid_cents=3000,
            amount_used_cents=1500,
            usage_covered_until=covered_until,
        )

        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(
                sub_id="sub_new", unit_amount=1000, metadata={"user_id": str(user.pk), "role_id": str(role.pk)}
            )
            webhooks.handle_event(
                {
                    "type": "invoice.payment_succeeded",
                    "data": {"object": {"subscription": "sub_new", "amount_paid": 1000}},
                }
            )

        subscription = RoleSubscription.objects.get(stripe_subscription_id="sub_new")
        self.assertEqual(subscription.total_paid_cents, 3000 + 1000)
        self.assertEqual(subscription.amount_used_cents, 1500)
        self.assertEqual(subscription.usage_covered_until, covered_until)

    def test_out_of_order_delivery_with_unresolvable_metadata_is_a_no_op(self) -> None:
        with mock.patch("stripe.Subscription.retrieve") as mock_retrieve:
            mock_retrieve.return_value.to_dict.return_value = _subscription_payload(sub_id="sub_new", metadata={})
            webhooks.handle_event(
                {
                    "type": "invoice.payment_succeeded",
                    "data": {"object": {"subscription": "sub_new", "amount_paid": 1000}},
                }
            )  # must not raise

        self.assertFalse(RoleSubscription.objects.filter(stripe_subscription_id="sub_new").exists())


class HandleInvoicePaymentFailedTests(TestCase):
    def test_marks_the_subscription_past_due(self) -> None:
        subscription = baker.make(
            RoleSubscription, stripe_subscription_id="sub_123", status=BillingSubscriptionStatus.ACTIVE
        )
        webhooks.handle_event({"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_123"}}})

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, BillingSubscriptionStatus.PAST_DUE)

    def test_unknown_subscription_is_a_no_op(self) -> None:
        webhooks.handle_event(
            {"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_unknown"}}}
        )  # must not raise


def _refund_object(refund_id: str, amount: int) -> mock.MagicMock:
    """A stand-in for a live ``stripe.Refund`` instance from ``stripe.Refund.list(...)``.

    Unlike the embedded ``charge.refunds.data`` entries (plain dicts straight
    off the webhook JSON), a real Stripe SDK object only supports ``.to_dict()``/
    attribute/``[]`` access - not ``.get()`` - so the handler must normalize each
    one before treating it like the embedded dicts.
    """
    obj = mock.MagicMock()
    obj.to_dict.return_value = {"id": refund_id, "amount": amount}
    return obj


def _charge_refunded_event(
    *,
    refunds: list[dict],
    event_id: str = "evt_ref_1",
    charge_id: str = "ch_1",
    invoice: str | None = "in_1",
    has_more: bool = False,
) -> dict:
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {"object": {"id": charge_id, "invoice": invoice, "refunds": {"data": refunds, "has_more": has_more}}},
    }


class HandleChargeRefundedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        self.sub = baker.make(
            RoleSubscription,
            role=self.role,
            stripe_subscription_id="sub_123",
            total_paid_cents=2000,
            amount_used_cents=500,
        )

    def _mock_invoice(self, subscription: str | None = "sub_123") -> mock.MagicMock:
        patcher = mock.patch("stripe.Invoice.retrieve")
        mock_retrieve = patcher.start()
        self.addCleanup(patcher.stop)
        mock_retrieve.return_value.to_dict.return_value = {"id": "in_1", "subscription": subscription}
        return mock_retrieve

    def test_decrements_the_banked_balance_by_the_refunded_amount(self) -> None:
        self._mock_invoice()
        webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}]))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1500)
        # Access already consumed is forgiven, not clawed back.
        self.assertEqual(self.sub.amount_used_cents, 500)

    def test_clamps_the_balance_at_zero(self) -> None:
        self._mock_invoice()
        webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 99_999}]))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)

    def test_redelivered_event_applies_each_refund_once(self) -> None:
        self._mock_invoice()
        event = _charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}])
        webhooks.handle_event(event)
        webhooks.handle_event(event)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1500)

    def test_cumulative_second_event_applies_only_the_new_refund(self) -> None:
        """charge.refunded delivers the charge's *cumulative* refund list - a second
        partial refund arrives as a fresh event (new event id) that re-contains the
        first refund, which must not be applied again."""
        self._mock_invoice()
        webhooks.handle_event(_charge_refunded_event(event_id="evt_ref_1", refunds=[{"id": "re_1", "amount": 500}]))
        webhooks.handle_event(
            _charge_refunded_event(
                event_id="evt_ref_2", refunds=[{"id": "re_1", "amount": 500}, {"id": "re_2", "amount": 300}]
            )
        )

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000 - 500 - 300)

    def test_charge_without_an_invoice_is_a_no_op(self) -> None:
        mock_retrieve = self._mock_invoice()
        webhooks.handle_event(_charge_refunded_event(invoice=None, refunds=[{"id": "re_1", "amount": 500}]))

        mock_retrieve.assert_not_called()
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_invoice_without_a_subscription_is_a_no_op(self) -> None:
        self._mock_invoice(subscription=None)
        webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}]))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_unknown_subscription_is_a_no_op(self) -> None:
        self._mock_invoice(subscription="sub_unknown")
        webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}]))  # must not raise

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_paginated_refund_list_fetches_and_applies_every_refund(self) -> None:
        """Regression: ``has_more`` used to only log a warning and truncate to the
        embedded page - Stripe caps ``charge.refunds.data`` at 10, so a charge with
        more refunds than that silently never had the rest debited from the banked
        balance. Since idempotency is keyed per refund id, a later redelivery of
        this same (still-truncated) embedded page never caught them up either -
        the loss was permanent. The full list must be fetched directly instead.
        """
        self._mock_invoice()
        with mock.patch("stripe.Refund.list") as mock_list:
            mock_list.return_value.auto_paging_iter.return_value = [
                _refund_object("re_1", 500),
                _refund_object("re_2", 300),
            ]
            webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}], has_more=True))
            mock_list.assert_called_once_with(charge="ch_1", limit=100)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000 - 500 - 300)

    def test_non_paginated_refund_list_never_calls_the_list_api(self) -> None:
        """The extra API round-trip is only worth paying when Stripe says there's
        more to fetch - the common case (a charge with a handful of refunds) must
        stay a single API call (the Invoice retrieve already mocked in setUp)."""
        self._mock_invoice()
        with mock.patch("stripe.Refund.list") as mock_list:
            webhooks.handle_event(_charge_refunded_event(refunds=[{"id": "re_1", "amount": 500}], has_more=False))
            mock_list.assert_not_called()

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1500)

    def test_missing_refund_list_does_not_raise(self) -> None:
        self._mock_invoice()
        event = {"id": "evt_ref_1", "type": "charge.refunded", "data": {"object": {"id": "ch_1", "invoice": "in_1"}}}
        webhooks.handle_event(event)  # must not raise

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)


class HandleChargeDisputeClosedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        self.sub = baker.make(RoleSubscription, role=self.role, stripe_subscription_id="sub_123", total_paid_cents=2000)

    def _event(self, *, status: str = "lost", amount: int = 500, charge: str | None = "ch_1") -> dict:
        return {
            "id": "evt_dp_1",
            "type": "charge.dispute.closed",
            "data": {"object": {"id": "dp_1", "status": status, "charge": charge, "amount": amount}},
        }

    def _mock_stripe(
        self, *, invoice: str | None = "in_1", subscription: str | None = "sub_123"
    ) -> tuple[mock.MagicMock, mock.MagicMock]:
        charge_patcher = mock.patch("stripe.Charge.retrieve")
        invoice_patcher = mock.patch("stripe.Invoice.retrieve")
        mock_charge = charge_patcher.start()
        mock_invoice = invoice_patcher.start()
        self.addCleanup(charge_patcher.stop)
        self.addCleanup(invoice_patcher.stop)
        mock_charge.return_value.to_dict.return_value = {"id": "ch_1", "invoice": invoice}
        mock_invoice.return_value.to_dict.return_value = {"id": "in_1", "subscription": subscription}
        return mock_charge, mock_invoice

    def test_lost_dispute_claws_back_the_disputed_amount(self) -> None:
        self._mock_stripe()
        webhooks.handle_event(self._event(status="lost", amount=500))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1500)

    def test_clamps_the_balance_at_zero(self) -> None:
        self._mock_stripe()
        webhooks.handle_event(self._event(status="lost", amount=99_999))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)

    def test_non_lost_dispute_is_ignored(self) -> None:
        mock_charge, _mock_invoice = self._mock_stripe()
        for status in ("won", "needs_response", "under_review", "warning_closed"):
            with self.subTest(status=status):
                webhooks.handle_event(self._event(status=status))

        mock_charge.assert_not_called()
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_charge_without_an_invoice_is_a_no_op(self) -> None:
        self._mock_stripe(invoice=None)
        webhooks.handle_event(self._event())

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_dispute_without_a_charge_is_a_no_op(self) -> None:
        mock_charge, _mock_invoice = self._mock_stripe()
        webhooks.handle_event(self._event(charge=None))

        mock_charge.assert_not_called()
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_invoice_without_a_subscription_is_a_no_op(self) -> None:
        self._mock_stripe(subscription=None)
        webhooks.handle_event(self._event())

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_unknown_subscription_is_a_no_op(self) -> None:
        self._mock_stripe(subscription="sub_unknown")
        webhooks.handle_event(self._event())  # must not raise

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)


class HandleEventUnknownTypeTests(TestCase):
    def test_unhandled_event_type_is_ignored(self) -> None:
        webhooks.handle_event({"type": "customer.updated", "data": {"object": {}}})  # must not raise
