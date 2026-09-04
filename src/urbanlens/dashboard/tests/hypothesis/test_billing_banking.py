"""Tests for services.billing.banking - the pay-what-you-want usage ledger that can
keep granting access after a subscription stops being actively billed."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import banking


def _set_created(role_subscription: RoleSubscription, created: object) -> None:
    RoleSubscription.objects.filter(pk=role_subscription.pk).update(created=created)
    role_subscription.refresh_from_db()


class AdvanceUsageLedgerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role)
        self.start = timezone.now() - timedelta(days=200)
        _set_created(self.sub, self.start)

    def test_worked_example_three_overpayments_then_cancel_bank_six_months(self) -> None:
        """$5/mo role, three $10 payments, then nothing - banks exactly 6 months from start."""
        for i in range(3):
            banking.apply_payment(self.sub, 1000, as_of=self.start + timedelta(days=30 * i))
        self.sub.refresh_from_db()
        # Only 2 periods reachable from the payments themselves (as_of caps how far a tick can
        # reach) - the daily sweep is what catches the remaining periods up as real time passes.
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=200))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 3000)
        self.assertEqual(self.sub.amount_used_cents, 3000)
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=180))

    def test_boundary_is_inclusive_exactly_enough_still_affords(self) -> None:
        """Paid exactly amount_used + threshold for the next period - still counts (>=, not >)."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=500)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=30))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.amount_used_cents, 500)
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=30))

    def test_one_cent_short_does_not_afford(self) -> None:
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=499)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=30))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.amount_used_cents, 0)
        self.assertIsNone(self.sub.usage_covered_until)

    def test_insufficient_balance_halts_without_partial_consumption(self) -> None:
        """Enough for one period but not two - stops cleanly at one, doesn't partially spend the second."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=700)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=90))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.amount_used_cents, 500)
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=30))

    def test_does_not_advance_past_the_period_containing_as_of(self) -> None:
        """Plenty of balance for many future periods, but ticking stops at the end of the
        period as_of currently falls in - it doesn't pre-consume periods that haven't
        started yet, even though the balance could easily afford them."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=100_000)
        self.sub.refresh_from_db()
        # as_of=45 days falls inside the second period ([30, 60)) - coverage should reach
        # that period's end (60 days), one period past as_of, but no further.
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=45))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=60))

    def test_non_pwyw_role_is_a_no_op(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=500)
        sub = baker.make(RoleSubscription, user=self.user, role=role, total_paid_cents=100_000)
        _set_created(sub, self.start)
        banking.advance_usage_ledger(sub, as_of=self.start + timedelta(days=90))
        sub.refresh_from_db()
        self.assertEqual(sub.amount_used_cents, 0)
        self.assertIsNone(sub.usage_covered_until)

    def test_period_start_exactly_at_as_of_is_entered(self) -> None:
        """cursor <= as_of, not cursor < as_of - a period counts as started the instant
        as_of reaches its start, not only strictly after. Balance is ample, so only this
        comparison decides whether the second period (starting exactly at as_of) is entered."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=100_000)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=30))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.amount_used_cents, 1000)
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=60))

    def test_period_starting_one_instant_after_as_of_is_not_entered(self) -> None:
        """One microsecond before the same boundary, that second period must not be
        entered yet - pins the other side of the cutoff pinned above."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=100_000)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub, as_of=self.start + timedelta(days=30) - timedelta(microseconds=1))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.amount_used_cents, 500)
        self.assertEqual(self.sub.usage_covered_until, self.start + timedelta(days=30))

    def test_price_change_only_affects_periods_ticked_after_the_change(self) -> None:
        """A dynamic-threshold role's cost can move between periods - each period is priced
        as of its own start, not the price in effect when advance_usage_ledger is finally called."""
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        sub = baker.make(RoleSubscription, user=baker.make(User), role=role, total_paid_cents=2000)
        _set_created(sub, self.start)

        def fake_threshold(evaluated_role: SubscriptionRole, as_of=None) -> int:
            # $5/mo for the first period, $10/mo from the second period onward.
            return 500 if as_of is not None and as_of < self.start + timedelta(days=30) else 1000

        with mock.patch(
            "urbanlens.dashboard.services.billing.banking.pricing.role_pwyw_threshold_cents", side_effect=fake_threshold
        ):
            banking.advance_usage_ledger(sub, as_of=self.start + timedelta(days=90))
        sub.refresh_from_db()
        # Period 1 costs 500 (total used 500, balance 1500 remaining), period 2 costs 1000 at
        # the new price (total used 1500, balance 500 remaining - not enough for a 3rd period).
        self.assertEqual(sub.amount_used_cents, 1500)
        self.assertEqual(sub.usage_covered_until, self.start + timedelta(days=60))


class ApplyPaymentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role)
        _set_created(self.sub, timezone.now() - timedelta(days=1))

    def test_adds_to_total_paid_and_persists(self) -> None:
        banking.apply_payment(self.sub, 1000)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1000)

    def test_ticks_the_ledger_after_recording_payment(self) -> None:
        _set_created(self.sub, timezone.now() - timedelta(days=31))
        banking.apply_payment(self.sub, 1000)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.has_banked_access)

    def test_two_payments_from_stale_instances_both_land(self) -> None:
        """Same lost-update shape as concurrent refunds, in the direction that credits."""
        first = RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)
        second = RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)

        banking.apply_payment(first, 1000)
        banking.apply_payment(second, 1000)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_non_pwyw_role_is_a_no_op(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=500)
        sub = baker.make(RoleSubscription, user=self.user, role=role)
        banking.apply_payment(sub, 1000)
        sub.refresh_from_db()
        self.assertEqual(sub.total_paid_cents, 0)

    def test_zero_amount_is_a_no_op(self) -> None:
        banking.apply_payment(self.sub, 0)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)

    def test_negative_amount_is_a_no_op(self) -> None:
        banking.apply_payment(self.sub, -500)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)

    def test_zero_threshold_pwyw_role_always_affords_once_paid(self) -> None:
        """A plain PWYW role with no minimum ticks for free every period once any payment lands."""
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=None
        )
        sub = baker.make(RoleSubscription, user=self.user, role=role)
        _set_created(sub, timezone.now() - timedelta(days=31))
        banking.apply_payment(sub, 1)
        sub.refresh_from_db()
        self.assertTrue(sub.has_banked_access)
        self.assertEqual(sub.amount_used_cents, 0)


class ApplyRefundTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role, total_paid_cents=2000)
        _set_created(self.sub, timezone.now() - timedelta(days=1))

    def test_subtracts_from_total_paid_and_persists(self) -> None:
        banking.apply_refund(self.sub, 500)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1500)

    def test_clamps_at_zero_when_refund_exceeds_balance(self) -> None:
        banking.apply_refund(self.sub, 5000)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)

    def test_consumed_periods_are_forgiven_not_reversed(self) -> None:
        """A refund stops future periods from being affordable but never claws back
        access the ledger already granted - amount_used_cents and usage_covered_until
        stay exactly where the last tick left them."""
        _set_created(self.sub, timezone.now() - timedelta(days=31))
        banking.advance_usage_ledger(self.sub)
        self.sub.refresh_from_db()
        used_before = self.sub.amount_used_cents
        covered_before = self.sub.usage_covered_until
        self.assertGreater(used_before, 0)

        banking.apply_refund(self.sub, 5000)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 0)
        self.assertEqual(self.sub.amount_used_cents, used_before)
        self.assertEqual(self.sub.usage_covered_until, covered_before)

    def test_two_refunds_from_stale_instances_both_land(self) -> None:
        """Stripe delivers concurrently, and both handlers hold their own instance.

        Two partial refunds on one charge arrive at once, each handler having
        read ``total_paid_cents`` before the other wrote. Subtracting from that
        in-memory value made the second write erase the first - while the
        webhook's StripeProcessedRefund row still committed, so the lost debit
        was never retried and access stayed funded by refunded money. Both
        instances here are deliberately stale, which is what that looks like
        without needing real threads.
        """
        first = RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)
        second = RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)

        banking.apply_refund(first, 300)
        banking.apply_refund(second, 300)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 1400)

    def test_a_refund_against_a_stale_instance_leaves_it_current(self) -> None:
        """The caller's object must not keep pre-lock values after the debit."""
        stale = RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)
        banking.apply_refund(self.sub, 500)

        banking.apply_refund(stale, 500)

        self.assertEqual(stale.total_paid_cents, 1000)

    def test_non_pwyw_role_is_a_no_op(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=500)
        sub = baker.make(RoleSubscription, user=self.user, role=role, total_paid_cents=2000)
        banking.apply_refund(sub, 500)
        sub.refresh_from_db()
        self.assertEqual(sub.total_paid_cents, 2000)

    def test_zero_amount_is_a_no_op(self) -> None:
        banking.apply_refund(self.sub, 0)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)

    def test_negative_amount_is_a_no_op(self) -> None:
        banking.apply_refund(self.sub, -500)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2000)


class RefundRoundTripPropertyTests(TestCase):
    """apply_payment then apply_refund of the same amount is a no-op on the banked
    balance, and never rewinds what the ledger already consumed."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role)
        _set_created(self.sub, timezone.now() - timedelta(days=45))

    @settings(max_examples=25, deadline=None)
    @given(initial_paid=st.integers(min_value=0, max_value=5000), amount=st.integers(min_value=1, max_value=10_000))
    def test_full_refund_restores_the_prior_banked_balance(self, initial_paid: int, amount: int) -> None:
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=initial_paid)
        self.sub.refresh_from_db()
        banking.advance_usage_ledger(self.sub)
        self.sub.refresh_from_db()
        balance_before = self.sub.total_paid_cents

        banking.apply_payment(self.sub, amount)
        self.sub.refresh_from_db()
        used_after_payment = self.sub.amount_used_cents
        covered_after_payment = self.sub.usage_covered_until

        banking.apply_refund(self.sub, amount)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, balance_before)
        # Consumed periods stay consumed - the refund forgives access already granted.
        self.assertEqual(self.sub.amount_used_cents, used_after_payment)
        self.assertEqual(self.sub.usage_covered_until, covered_after_payment)
