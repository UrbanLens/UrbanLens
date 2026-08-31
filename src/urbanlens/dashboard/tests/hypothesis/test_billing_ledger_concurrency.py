"""The pay-what-you-want ledger must not be advanced from a stale snapshot.

``apply_payment`` and ``advance_usage_ledger`` are read-modify-write cycles over
``total_paid_cents`` / ``amount_used_cents`` / ``usage_covered_until``: they read
the values off the in-memory ``RoleSubscription`` they were handed, compute new
ones, and ``save(update_fields=...)`` the result. Whoever writes last wins, so
any caller holding a snapshot taken before someone else's write silently undoes
it - the classic lost update, here over money.

The webhook receiver's replay protection does not cover this. Its
``select_for_update`` is keyed on the *event id*, so it serializes redeliveries
of one event and nothing else; two different ``invoice.payment_succeeded``
events for the same subscription take two different locks and run concurrently.
The daily ``advance_pwyw_usage_ledgers`` sweep takes no lock at all, and holds
each row's snapshot for as long as its loop takes to reach that row.

These tests model the interleaving directly rather than with threads: two
instances loaded from the same row *are* the two workers' snapshots, and driving
them in sequence reproduces exactly the write-after-stale-read that concurrency
would produce. That makes the failure deterministic, at the cost of not
exercising the database's own locking - what is under test is that these
functions refuse to trust a snapshot, which is the part that was wrong.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import banking


class LedgerStaleSnapshotTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500)
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role)
        self.start = timezone.now() - timedelta(days=200)
        RoleSubscription.objects.filter(pk=self.sub.pk).update(created=self.start)
        self.sub.refresh_from_db()

    def _snapshot(self) -> RoleSubscription:
        """A second worker's copy of the row, read before the first one writes."""
        return RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)

    def test_two_concurrent_payments_are_both_credited(self) -> None:
        """A user who pays twice must be credited twice - this is real money."""
        first, second = self._snapshot(), self._snapshot()

        banking.apply_payment(first, 1000, as_of=self.start)
        banking.apply_payment(second, 2000, as_of=self.start)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 3000, "a concurrent payment overwrote the other one's credit")

    def test_a_payment_is_not_rolled_back_by_the_daily_sweep(self) -> None:
        """The sweep reads every PWYW row, then advances them one at a time.

        A payment landing inside that window must survive. The sweep's snapshot
        predates the payment, so writing ``usage_covered_until`` from it moves
        coverage *backwards* - the user paid for time and then lost it.

        Note the direction: two overlapping *sweeps* are harmless, because
        ``advance_usage_ledger`` is a pure function of the snapshot it is given
        and two identical snapshots compute identical writes. The damage needs a
        writer the stale reader hasn't seen, which is what a payment is.
        """
        # Small first payment, so the stale snapshot cannot afford the periods the
        # larger one below pays for - otherwise both compute the same coverage and
        # the lost update, while still a lost update, does no visible damage.
        banking.apply_payment(self._snapshot(), 1000, as_of=self.start)
        sweep_snapshot = self._snapshot()

        banking.apply_payment(self._snapshot(), 9000, as_of=self.start + timedelta(days=60))
        paid_until = RoleSubscription.objects.get(pk=self.sub.pk).usage_covered_until

        banking.advance_usage_ledger(sweep_snapshot, as_of=self.start + timedelta(days=60))

        self.sub.refresh_from_db()
        self.assertGreaterEqual(self.sub.usage_covered_until, paid_until, "the sweep un-advanced coverage a payment had already bought")

    def test_the_caller_s_instance_reflects_what_was_stored(self) -> None:
        """Refreshing under the lock must leave the caller holding current values.

        ``_handle_invoice_payment_succeeded`` reads nothing after ``apply_payment``
        today, but an instance that silently disagrees with the row it came from
        is a trap for the next caller.
        """
        subscription = self._snapshot()
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=750)

        banking.apply_payment(subscription, 250, as_of=self.start)

        self.assertEqual(subscription.total_paid_cents, 1000)
        self.assertEqual(RoleSubscription.objects.get(pk=self.sub.pk).total_paid_cents, 1000)

    def test_advance_usage_ledger_syncs_the_caller_s_instance(self) -> None:
        """The same trap as above, for the two fields ``advance_usage_ledger`` itself
        writes - the prior test only ever checks ``total_paid_cents`` on the caller's
        object, which ``apply_payment`` writes directly. ``amount_used_cents`` and
        ``usage_covered_until`` are only ever set on ``locked``, so a sync-back that
        silently dropped them from the post-lock copy loop would pass every other
        test in this file while leaving direct callers holding stale coverage.
        """
        subscription = self._snapshot()
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=100_000)

        banking.advance_usage_ledger(subscription, as_of=self.start + timedelta(days=30))

        self.assertEqual(subscription.amount_used_cents, 1000)
        self.assertEqual(subscription.usage_covered_until, self.start + timedelta(days=60))

    def test_a_payment_and_a_refund_do_not_erase_each_other(self) -> None:
        """The mixed interleaving ``_locked``'s own docstring names as motivation -
        "a payment landing beside a refund" - and that no test here (or in the
        payment-only / refund-only stale-snapshot tests elsewhere) actually drives.
        """
        RoleSubscription.objects.filter(pk=self.sub.pk).update(total_paid_cents=2000)
        payer, refunder = self._snapshot(), self._snapshot()

        banking.apply_payment(payer, 1000, as_of=self.start)
        banking.apply_refund(refunder, 500)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 2500, "a concurrent payment and refund erased each other instead of both applying")
