"""Two payments crediting the same subscription at once must both be kept."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import banking


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LedgerLockTests(TransactionTestCase):
    """Cache pinned to locmem and background dispatch stubbed, as in the other race tests."""

    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.subscription = baker.make(RoleSubscription, user=baker.make(User), role=self.role)

    def _pay(self, amount: int):
        """A worker crediting a payment, on its own connection."""

        def credit() -> None:
            subscription = RoleSubscription.objects.select_related("role").get(pk=self.subscription.pk)
            banking.apply_payment(subscription, amount)

        return credit

    def test_two_concurrent_payments_are_both_credited(self) -> None:
        run_concurrently([self._pay(1000), self._pay(2000)])

        self.subscription.refresh_from_db()
        self.assertEqual(
            self.subscription.total_paid_cents, 3000, "a concurrent payment was lost - the ledger row was not locked"
        )

    def test_many_concurrent_payments_all_land(self) -> None:
        """Four writers, because two can pass by luck of scheduling."""
        run_concurrently([self._pay(500) for _ in range(4)])

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 2000, "at least one of four concurrent payments was lost")

    def test_a_single_payment_is_unaffected(self) -> None:
        """The uncontended path the locking must not change."""
        run_concurrently([self._pay(750)])

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 750)
