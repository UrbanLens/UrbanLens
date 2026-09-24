"""The nightly Stripe re-sync pages the list API, and the ledger sweep skips rows it cannot advance (G6-13, G2-36)."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.UrbanLens.settings.app import settings as app_settings


def _subscription(sub_id: str, status: str = "active") -> dict:
    return {
        "id": sub_id,
        "status": status,
        "cancel_at_period_end": False,
        "canceled_at": None,
        "items": {"data": [{"current_period_end": 1_800_000_000, "price": {"id": "price_1", "unit_amount": 1000}}]},
    }


def _page(*payloads: dict, has_more: bool = False) -> mock.Mock:
    return mock.Mock(data=[mock.Mock(to_dict=lambda p=p: p) for p in payloads], has_more=has_more)


class StripeSyncTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        configured = mock.patch.object(app_settings, "stripe_secret_key", "sk_test_123")
        configured.start()
        self.addCleanup(configured.stop)
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        self.role = baker.make(SubscriptionRole)

    def _row(self, sub_id: str, status: str = BillingSubscriptionStatus.ACTIVE, **extra) -> RoleSubscription:
        return baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=self.role,
            stripe_subscription_id=sub_id,
            status=status,
            **extra,
        )

    def _enqueued(self, task) -> list[tuple]:
        return [c.args[1:] for c in self.enqueue.call_args_list if c.args[0] is task]


class SyncPageTests(StripeSyncTestCase):
    def test_one_page_is_applied_from_the_list_without_per_row_retrieves(self) -> None:
        row = self._row("sub_a")
        with (
            mock.patch(
                "stripe.Subscription.list",
                return_value=_page(_subscription("sub_a", "past_due"), _subscription("sub_foreign")),
            ) as listed,
            mock.patch("stripe.Subscription.retrieve") as retrieve,
        ):
            applied = tasks.sync_stripe_subscriptions()

        self.assertEqual(applied, 1)
        self.assertEqual(listed.call_args.kwargs["limit"], 100)
        self.assertNotIn("starting_after", listed.call_args.kwargs)
        retrieve.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, "past_due")

    def test_a_full_page_hands_the_next_page_to_its_own_task(self) -> None:
        """A retry after a failed page re-runs that page only, not every page before it."""
        self._row("sub_a")
        with mock.patch(
            "stripe.Subscription.list",
            return_value=_page(_subscription("sub_a"), _subscription("sub_b"), has_more=True),
        ):
            tasks.sync_stripe_subscriptions()

        [(starting_after, sweep_started_at)] = self._enqueued(tasks.sync_stripe_subscriptions)
        self.assertEqual(starting_after, "sub_b")
        self.assertIsInstance(sweep_started_at, float)
        self.assertEqual(self._enqueued(tasks.reconcile_unlisted_stripe_subscriptions), [])

    def test_a_later_page_continues_from_its_cursor_and_keeps_the_sweep_start(self) -> None:
        with mock.patch("stripe.Subscription.list", return_value=_page()) as listed:
            tasks.sync_stripe_subscriptions("sub_b", 1_700_000_000.0)

        self.assertEqual(listed.call_args.kwargs["starting_after"], "sub_b")
        self.assertEqual(self._enqueued(tasks.reconcile_unlisted_stripe_subscriptions), [(1_700_000_000.0, 0)])

    def test_the_last_page_hands_off_to_reconciliation(self) -> None:
        with mock.patch("stripe.Subscription.list", return_value=_page(_subscription("sub_a"))):
            tasks.sync_stripe_subscriptions()

        [(sweep_started_at, after_pk)] = self._enqueued(tasks.reconcile_unlisted_stripe_subscriptions)
        self.assertEqual(after_pk, 0)
        self.assertLessEqual(sweep_started_at, timezone.now().timestamp())

    def test_a_fresh_worker_configures_the_api_key_before_listing(self) -> None:
        import stripe

        seen: list[str | None] = []

        def listed(**_params: object) -> mock.Mock:
            seen.append(stripe.api_key)
            return _page()

        with mock.patch.object(stripe, "api_key", None), mock.patch("stripe.Subscription.list", side_effect=listed):
            tasks.sync_stripe_subscriptions()

        self.assertEqual(seen, ["sk_test_123"])

    def test_unconfigured_stripe_is_a_no_op(self) -> None:
        with (
            mock.patch.object(app_settings, "stripe_secret_key", None),
            mock.patch("stripe.Subscription.list") as listed,
        ):
            self.assertEqual(tasks.sync_stripe_subscriptions(), 0)

        listed.assert_not_called()


class ReconcileTests(StripeSyncTestCase):
    def test_only_rows_the_listing_did_not_reach_are_retrieved(self) -> None:
        """The default listing omits canceled subscriptions, so a missed ``deleted`` webhook surfaces here."""
        sweep_started = timezone.now() - timedelta(minutes=10)
        self._row("sub_listed", stripe_state_at=sweep_started + timedelta(minutes=1))
        missed = self._row("sub_missed", stripe_state_at=sweep_started - timedelta(days=1))
        never = self._row("sub_never")
        self._row("sub_done", status=BillingSubscriptionStatus.CANCELED)

        def retrieve(sub_id: str) -> mock.Mock:
            return mock.Mock(to_dict=lambda: _subscription(sub_id, "canceled"))

        with mock.patch("stripe.Subscription.retrieve", side_effect=retrieve) as retrieved:
            tasks.reconcile_unlisted_stripe_subscriptions(sweep_started.timestamp(), 0)

        self.assertEqual(sorted(c.args[0] for c in retrieved.call_args_list), ["sub_missed", "sub_never"])
        for row in (missed, never):
            row.refresh_from_db()
            self.assertEqual(row.status, BillingSubscriptionStatus.CANCELED)
        self.assertEqual(self._enqueued(tasks.reconcile_unlisted_stripe_subscriptions), [])

    def test_a_full_chunk_hands_the_rest_to_its_own_task(self) -> None:
        rows = [self._row(f"sub_{n}") for n in range(3)]
        with mock.patch(
            "stripe.Subscription.retrieve", side_effect=lambda sub_id: mock.Mock(to_dict=lambda: _subscription(sub_id))
        ) as retrieved:
            tasks.reconcile_unlisted_stripe_subscriptions(timezone.now().timestamp(), 0, 2)

        self.assertEqual(retrieved.call_count, 2)
        [(_started, after_pk, chunk)] = self._enqueued(tasks.reconcile_unlisted_stripe_subscriptions)
        self.assertEqual(after_pk, rows[1].pk)
        self.assertEqual(chunk, 2)

    def test_one_failing_retrieve_does_not_stop_the_chunk(self) -> None:
        import stripe

        self._row("sub_bad")
        good = self._row("sub_good")

        def retrieve(sub_id: str) -> mock.Mock:
            if sub_id == "sub_bad":
                raise stripe.InvalidRequestError("No such subscription", param="id")
            return mock.Mock(to_dict=lambda: _subscription(sub_id, "past_due"))

        with mock.patch("stripe.Subscription.retrieve", side_effect=retrieve):
            tasks.reconcile_unlisted_stripe_subscriptions(timezone.now().timestamp(), 0)

        good.refresh_from_db()
        self.assertEqual(good.status, "past_due")


class LedgerSweepFilterTests(TestCase):
    def test_only_ledgers_that_can_advance_are_visited(self) -> None:
        now = timezone.now()
        static = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        free = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=None
        )
        dynamic = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True)

        def row(role: SubscriptionRole, **fields) -> RoleSubscription:
            return baker.make(RoleSubscription, user=baker.make(User), role=role, **fields)

        covered_ahead = row(
            static, total_paid_cents=5000, amount_used_cents=0, usage_covered_until=now + timedelta(days=3)
        )
        exhausted = row(
            static, total_paid_cents=1200, amount_used_cents=1000, usage_covered_until=now - timedelta(days=1)
        )
        due = row(static, total_paid_cents=1500, amount_used_cents=1000, usage_covered_until=now - timedelta(days=1))
        never_advanced = row(static, total_paid_cents=500)
        free_row = row(free, usage_covered_until=now - timedelta(days=1))
        dynamic_row = row(dynamic, usage_covered_until=now - timedelta(days=1))

        with mock.patch("urbanlens.dashboard.services.billing.banking.advance_usage_ledger") as advance:
            count = tasks.advance_pwyw_usage_ledgers()

        visited = {c.args[0].pk for c in advance.call_args_list}
        self.assertEqual(visited, {due.pk, never_advanced.pk, free_row.pk, dynamic_row.pk})
        self.assertNotIn(covered_ahead.pk, visited)
        self.assertNotIn(exhausted.pk, visited)
        self.assertEqual(count, 4)
