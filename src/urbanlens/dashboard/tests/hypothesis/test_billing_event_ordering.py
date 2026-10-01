"""Stripe state is applied in Stripe's order, terminal states stick, and creates are idempotent (G2-16, G2-17).

Stripe guarantees neither ordering nor single delivery. Every handler here runs sequentially on one connection; the
concurrent versions live in ``test_billing_stripe_races.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingCustomer, BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import stripe_client, webhooks
from urbanlens.UrbanLens.settings.app import settings as app_settings

_T0 = 1_700_000_000


def _configured():
    return mock.patch.object(app_settings, "stripe_secret_key", "sk_test_123")


def _subscription(
    sub_id: str = "sub_1", status: str = "active", unit_amount: int = 1000, metadata: dict | None = None
) -> dict:
    return {
        "id": sub_id,
        "status": status,
        "cancel_at_period_end": False,
        "canceled_at": None,
        "metadata": metadata or {},
        "items": {
            "data": [{"current_period_end": 1_800_000_000, "price": {"id": "price_1", "unit_amount": unit_amount}}]
        },
    }


def _event(event_type: str, obj: dict, created: int | None) -> dict:
    event: dict[str, object] = {"type": event_type, "data": {"object": obj}}
    if created is not None:
        event["created"] = created
    return event


class EventOrderingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.row = baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=baker.make(SubscriptionRole),
            stripe_subscription_id="sub_1",
            status=BillingSubscriptionStatus.ACTIVE,
            pledged_amount_cents=1000,
        )

    def test_an_older_update_delivered_late_is_ignored(self) -> None:
        webhooks.handle_event(
            _event("customer.subscription.updated", _subscription(status="past_due", unit_amount=1500), _T0 + 10)
        )
        webhooks.handle_event(
            _event("customer.subscription.updated", _subscription(status="active", unit_amount=900), _T0)
        )

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "past_due")
        self.assertEqual(self.row.pledged_amount_cents, 1500)
        self.assertEqual(self.row.stripe_state_at, datetime.fromtimestamp(_T0 + 10, tz=UTC))

    def test_a_newer_update_applies(self) -> None:
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="past_due"), _T0))
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0 + 10))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "active")

    def test_same_second_events_both_apply_in_arrival_order(self) -> None:
        """``created`` has one-second resolution, so a tie cannot be ordered and must not be dropped."""
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="past_due"), _T0))
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "active")

    def test_an_update_older_than_the_cancellation_cannot_resurrect_it(self) -> None:
        webhooks.handle_event(_event("customer.subscription.deleted", _subscription(status="canceled"), _T0 + 10))
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, BillingSubscriptionStatus.CANCELED)

    def test_a_cancellation_is_terminal_even_against_a_later_timestamp(self) -> None:
        """Stripe never reactivates a canceled subscription; a later-stamped non-canceled payload is a misdelivery."""
        webhooks.handle_event(_event("customer.subscription.deleted", _subscription(status="canceled"), _T0))
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0 + 10))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, BillingSubscriptionStatus.CANCELED)

    def test_a_late_cancellation_still_cancels(self) -> None:
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0 + 10))
        webhooks.handle_event(_event("customer.subscription.deleted", _subscription(status="canceled"), _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, BillingSubscriptionStatus.CANCELED)

    def test_a_payment_failure_after_cancellation_does_not_reopen_the_row(self) -> None:
        webhooks.handle_event(_event("customer.subscription.deleted", _subscription(status="canceled"), _T0))
        webhooks.handle_event(_event("invoice.payment_failed", {"subscription": "sub_1"}, _T0 + 10))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, BillingSubscriptionStatus.CANCELED)

    def test_a_stale_payment_failure_does_not_override_a_newer_recovery(self) -> None:
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0 + 10))
        webhooks.handle_event(_event("invoice.payment_failed", {"subscription": "sub_1"}, _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "active")

    def test_an_event_without_created_still_applies(self) -> None:
        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="past_due"), None))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "past_due")

    def test_an_event_older_than_a_live_retrieve_is_ignored(self) -> None:
        """A retrieve reads the state as of when it was sent; an event created before that is already reflected."""
        retrieve_sent = timezone.now().replace(microsecond=0)
        with mock.patch("stripe.Subscription.retrieve") as retrieve:
            retrieve.return_value.to_dict.return_value = _subscription(status="active", unit_amount=2000)
            webhooks.handle_event(
                _event("invoice.payment_succeeded", {"subscription": "sub_1", "amount_paid": 2000}, None)
            )
        stale = int((retrieve_sent - timedelta(minutes=5)).timestamp())

        webhooks.handle_event(
            _event("customer.subscription.updated", _subscription(status="past_due", unit_amount=1000), stale)
        )

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "active")
        self.assertEqual(self.row.pledged_amount_cents, 2000)


class IncompleteExpiredIsTerminalTests(TestCase):
    def test_a_new_subscription_after_an_expired_one_is_recorded(self) -> None:
        """``incomplete_expired`` is as final as ``canceled``; the one-live-row constraint must not count it."""
        user = baker.make(User)
        role = baker.make(SubscriptionRole)
        baker.make(
            RoleSubscription,
            user=user,
            role=role,
            stripe_subscription_id="sub_old",
            status=BillingSubscriptionStatus.INCOMPLETE_EXPIRED,
        )
        session = {
            "mode": "subscription",
            "subscription": "sub_new",
            "client_reference_id": str(user.pk),
            "customer": "cus_1",
            "metadata": {"role_id": str(role.pk)},
        }

        with mock.patch("stripe.Subscription.retrieve") as retrieve:
            retrieve.return_value.to_dict.return_value = _subscription(sub_id="sub_new")
            webhooks.handle_event(_event("checkout.session.completed", session, _T0))

        self.assertEqual(RoleSubscription.objects.get(stripe_subscription_id="sub_new").status, "active")

    def test_an_expired_row_does_not_move(self) -> None:
        row = baker.make(
            RoleSubscription, stripe_subscription_id="sub_1", status=BillingSubscriptionStatus.INCOMPLETE_EXPIRED
        )

        webhooks.handle_event(_event("customer.subscription.updated", _subscription(status="active"), _T0))

        row.refresh_from_db()
        self.assertEqual(row.status, BillingSubscriptionStatus.INCOMPLETE_EXPIRED)


@override_settings(SITE_URL="https://one.example.test")
class IdempotencyKeyTests(TestCase):
    def _customer_key(self, user: User) -> str:
        with _configured(), mock.patch("stripe.Customer.create") as create:
            create.return_value = mock.Mock(id=f"cus_{user.pk}")
            stripe_client.ensure_customer(user)
        return create.call_args.kwargs["idempotency_key"]

    def test_a_retried_customer_create_reuses_its_key(self) -> None:
        """A create whose local insert rolled back must return the same Stripe customer on retry."""
        user = baker.make(User, email="a@example.test")
        first = self._customer_key(user)
        BillingCustomer.objects.filter(user=user).delete()

        self.assertEqual(self._customer_key(user), first)

    def test_customer_keys_differ_per_user_and_per_deployment(self) -> None:
        user = baker.make(User, email="a@example.test")
        other = baker.make(User, email="a@example.test")
        key = self._customer_key(user)
        self.assertNotEqual(key, self._customer_key(other))
        BillingCustomer.objects.filter(user=user).delete()

        with override_settings(SITE_URL="https://two.example.test"):
            self.assertNotEqual(key, self._customer_key(user), "deployments sharing a Stripe account would collide")

    def test_customer_key_changes_with_the_parameters(self) -> None:
        """Stripe rejects a reused key carrying different parameters, so a renamed user needs a new key."""
        user = baker.make(User, email="a@example.test")
        first = self._customer_key(user)
        BillingCustomer.objects.filter(user=user).delete()
        User.objects.filter(pk=user.pk).update(email="b@example.test")
        user.refresh_from_db()

        self.assertNotEqual(self._customer_key(user), first)

    def test_product_create_carries_a_stable_key(self) -> None:
        role = baker.make(SubscriptionRole, stripe_product_id="")
        keys = []
        for _attempt in range(2):
            SubscriptionRole.objects.filter(pk=role.pk).update(stripe_product_id="")
            role.refresh_from_db()
            with _configured(), mock.patch("stripe.Product.create") as create:
                create.return_value = mock.Mock(id="prod_1")
                stripe_client.ensure_product(role)
            keys.append(create.call_args.kwargs["idempotency_key"])

        self.assertEqual(keys[0], keys[1])
        self.assertTrue(keys[0])


class DuplicateSubscriptionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, monthly_price_cents=500, stripe_product_id="prod_1")
        baker.make(BillingCustomer, user=self.user, stripe_customer_id="cus_1")
        self.live = baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            stripe_subscription_id="sub_live",
            status=BillingSubscriptionStatus.ACTIVE,
        )

    def test_checkout_is_refused_for_a_role_already_held(self) -> None:
        with (
            _configured(),
            mock.patch("stripe.checkout.Session.create") as create,
            pytest.raises(stripe_client.AlreadySubscribedError),
        ):
            stripe_client.create_checkout_session(self.user, self.role, 500, "https://x.test/s", "https://x.test/c")

        create.assert_not_called()

    def test_checkout_is_allowed_after_the_old_subscription_ended(self) -> None:
        RoleSubscription.objects.filter(pk=self.live.pk).update(status=BillingSubscriptionStatus.CANCELED)
        with _configured(), mock.patch("stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(url="https://checkout.stripe.test/x")
            stripe_client.create_checkout_session(self.user, self.role, 500, "https://x.test/s", "https://x.test/c")

        create.assert_called_once()

    def test_the_checkout_view_redirects_instead_of_charging_twice(self) -> None:
        self.client.force_login(self.user)
        with _configured(), mock.patch("stripe.checkout.Session.create") as create:
            response = self.client.post(reverse("settings.billing.checkout"), {"role_slug": self.role.slug})

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("stripe", response["Location"])
        create.assert_not_called()

    def test_the_settings_section_marks_held_roles(self) -> None:
        self.client.force_login(self.user)
        with _configured():
            response = self.client.get(reverse("settings.billing"))

        row = next(r for r in response.context["role_rows"] if r["role"].pk == self.role.pk)
        self.assertTrue(row["already_subscribed"])

    def _complete_second_checkout(self, live_status_at_stripe: str) -> mock.MagicMock:
        session = {
            "mode": "subscription",
            "subscription": "sub_dup",
            "client_reference_id": str(self.user.pk),
            "customer": "cus_1",
            "metadata": {"role_id": str(self.role.pk)},
        }
        payloads = {
            "sub_dup": _subscription(sub_id="sub_dup"),
            "sub_live": _subscription(sub_id="sub_live", status=live_status_at_stripe),
        }

        def retrieve(sub_id: str) -> mock.Mock:
            return mock.Mock(to_dict=lambda: payloads[sub_id])

        with (
            mock.patch("stripe.Subscription.retrieve", side_effect=retrieve),
            mock.patch("stripe.Subscription.cancel") as cancel,
        ):
            cancel.return_value.to_dict.return_value = _subscription(sub_id="sub_dup", status="canceled")
            webhooks.handle_event(_event("checkout.session.completed", session, _T0))
        return cancel

    def test_a_second_live_subscription_is_canceled_not_crashed_on(self) -> None:
        """Two open checkout tabs can both complete; the second must not 500 the webhook while Stripe keeps billing it."""
        cancel = self._complete_second_checkout(live_status_at_stripe="active")

        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], "sub_dup")
        self.assertIn("idempotency_key", cancel.call_args.kwargs)
        self.assertFalse(RoleSubscription.objects.filter(stripe_subscription_id="sub_dup").exists())
        self.live.refresh_from_db()
        self.assertEqual(self.live.status, "active")

    def test_a_stale_live_row_is_refreshed_before_calling_the_new_one_a_duplicate(self) -> None:
        """A missed ``deleted`` webhook leaves a row that looks live; the new subscription is the real one."""
        cancel = self._complete_second_checkout(live_status_at_stripe="canceled")

        cancel.assert_not_called()
        self.live.refresh_from_db()
        self.assertEqual(self.live.status, BillingSubscriptionStatus.CANCELED)
        self.assertEqual(RoleSubscription.objects.get(stripe_subscription_id="sub_dup").status, "active")


class CurrentApiShapeTests(TestCase):
    """The SDK pins API ``2026-06-24.dahlia``: invoices name their subscription under ``parent``, charges carry no
    ``invoice`` and no embedded ``refunds``. Webhook payloads follow the endpoint's configured version, so both shapes
    must resolve."""

    def setUp(self) -> None:
        super().setUp()
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.row = baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=self.role,
            stripe_subscription_id="sub_1",
            status=BillingSubscriptionStatus.ACTIVE,
            total_paid_cents=3000,
        )

    @staticmethod
    def _invoice(amount_paid: int = 0) -> dict:
        return {
            "id": "in_1",
            "amount_paid": amount_paid,
            "parent": {"type": "subscription_details", "subscription_details": {"subscription": "sub_1"}},
        }

    def test_a_payment_on_a_current_shape_invoice_is_banked(self) -> None:
        with mock.patch("stripe.Subscription.retrieve") as retrieve:
            retrieve.return_value.to_dict.return_value = _subscription()
            webhooks.handle_event(_event("invoice.payment_succeeded", self._invoice(amount_paid=1000), _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.total_paid_cents, 4000)

    def test_a_failed_payment_on_a_current_shape_invoice_marks_past_due(self) -> None:
        webhooks.handle_event(_event("invoice.payment_failed", self._invoice(), _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, BillingSubscriptionStatus.PAST_DUE)

    def _invoice_lookups(self):
        payments = mock.Mock(data=[mock.Mock(to_dict=lambda: {"invoice": "in_1"})])
        return (
            mock.patch("stripe.InvoicePayment.list", return_value=payments),
            mock.patch("stripe.Invoice.retrieve", return_value=mock.Mock(to_dict=self._invoice)),
        )

    def test_a_refund_on_a_current_shape_charge_is_debited(self) -> None:
        charge = {"id": "ch_1", "payment_intent": "pi_1"}
        refunds = [mock.Mock(to_dict=lambda: {"id": "re_1", "amount": 700, "status": "succeeded"})]
        list_payments, retrieve_invoice = self._invoice_lookups()
        with list_payments as listed, retrieve_invoice, mock.patch("stripe.Refund.list") as refund_list:
            refund_list.return_value.auto_paging_iter.return_value = refunds
            webhooks.handle_event(_event("charge.refunded", charge, _T0))

        self.assertEqual(listed.call_args.kwargs["payment"], {"type": "payment_intent", "payment_intent": "pi_1"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.total_paid_cents, 2300)

    def test_a_failed_refund_is_not_debited(self) -> None:
        charge = {
            "id": "ch_1",
            "invoice": "in_1",
            "refunds": {"data": [{"id": "re_1", "amount": 700, "status": "failed"}], "has_more": False},
        }
        with mock.patch("stripe.Invoice.retrieve", return_value=mock.Mock(to_dict=self._invoice)):
            webhooks.handle_event(_event("charge.refunded", charge, _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.total_paid_cents, 3000)

    def test_a_lost_dispute_on_a_current_shape_charge_is_debited(self) -> None:
        dispute = {"status": "lost", "charge": "ch_1", "amount": 1000}
        list_payments, retrieve_invoice = self._invoice_lookups()
        with list_payments, retrieve_invoice, mock.patch("stripe.Charge.retrieve") as charge:
            charge.return_value.to_dict.return_value = {"id": "ch_1", "payment_intent": "pi_1"}
            webhooks.handle_event(_event("charge.dispute.closed", dispute, _T0))

        self.row.refresh_from_db()
        self.assertEqual(self.row.total_paid_cents, 2000)
