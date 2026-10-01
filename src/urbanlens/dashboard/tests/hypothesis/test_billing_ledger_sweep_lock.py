"""A payment landing while the daily sweep advances the same ledger must not be rewound by it (P57).

`test_billing_ledger_lock.py` reaches `advance_usage_ledger` only through `apply_payment`, whose outer lock already
holds the row, so the inner lock is never the one deciding. The sweep (`advance_pwyw_usage_ledgers`) calls it
directly, and there its own lock is all that stands between a stale read and a concurrent payment. Here the sweep
pauses between that read and its save while a payment on another connection tries to land.
"""

from __future__ import annotations

from datetime import timedelta
import threading
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import banking, pricing

_PAUSE_SECONDS = 1


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SweepLedgerLockTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.subscription = baker.make(RoleSubscription, user=baker.make(User), role=role)
        self.start = timezone.now() - timedelta(days=45)
        RoleSubscription.objects.filter(pk=self.subscription.pk).update(created=self.start, total_paid_cents=500)

    def test_a_payment_during_the_sweep_waits_for_it_and_is_not_rewound(self) -> None:
        sweep_reading = threading.Event()
        payment_done = threading.Event()
        sweep_thread: list[int] = []
        payment_landed_mid_sweep: list[bool] = []
        real_threshold = pricing.role_pwyw_threshold_cents

        def threshold(*args: Any, **kwargs: Any) -> int | None:
            if threading.get_ident() in sweep_thread and not sweep_reading.is_set():
                sweep_reading.set()
                payment_landed_mid_sweep.append(payment_done.wait(timeout=_PAUSE_SECONDS))
            return real_threshold(*args, **kwargs)

        def sweep() -> None:
            sweep_thread.append(threading.get_ident())
            tasks.advance_pwyw_usage_ledgers()

        def pay() -> None:
            self.assertTrue(sweep_reading.wait(timeout=10), "the sweep never reached the ledger")
            subscription = RoleSubscription.objects.select_related("role").get(pk=self.subscription.pk)
            banking.apply_payment(subscription, 1000)
            payment_done.set()

        with mock.patch.object(pricing, "role_pwyw_threshold_cents", side_effect=threshold):
            run_concurrently([sweep, pay])

        self.assertEqual(payment_landed_mid_sweep, [False], "the payment was not held off while the sweep had the row")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 1500)
        self.assertEqual(self.subscription.amount_used_cents, 1000)
        self.assertEqual(
            self.subscription.usage_covered_until,
            self.start + timedelta(days=60),
            "the sweep saved its stale read over coverage the payment had bought",
        )
