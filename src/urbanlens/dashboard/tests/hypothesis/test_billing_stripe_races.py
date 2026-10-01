"""Concurrent billing work must not create duplicate Stripe objects or undo a cancellation (G2-16, G2-17).

Each scenario runs two callers on their own threads and connections. A patched hook holds the first caller between
its read and its write for a bounded pause; without the row lock the second caller runs inside that window, with it
the second caller waits it out.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.dashboard.models.billing import BillingCustomer, BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import pricing, stripe_client, webhooks
from urbanlens.UrbanLens.settings.app import settings as app_settings

_PAUSE_SECONDS = 1


def _held_for_the_other_caller(barrier: threading.Barrier, make_result: Any) -> Any:
    def held(*_args: Any, **_kwargs: Any) -> Any:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return make_result()

    return held


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class StripeRaceTestCase(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        configured = mock.patch.object(app_settings, "stripe_secret_key", "sk_test_123")
        configured.start()
        self.addCleanup(configured.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion


class EnsureCustomerRaceTests(StripeRaceTestCase):
    def test_two_concurrent_checkouts_create_one_stripe_customer(self) -> None:
        user = baker.make(User, email="person@example.test")
        ids = itertools.count(1)
        barrier = threading.Barrier(2, timeout=_PAUSE_SECONDS)
        create = mock.MagicMock(
            side_effect=_held_for_the_other_caller(barrier, lambda: mock.Mock(id=f"cus_{next(ids)}"))
        )

        with mock.patch("stripe.Customer.create", create):
            results = run_concurrently([lambda: stripe_client.ensure_customer(user).stripe_customer_id] * 2)

        self.assertEqual(create.call_count, 1, "both callers created a Stripe customer; one is orphaned")
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            list(BillingCustomer.objects.filter(user=user).values_list("stripe_customer_id", flat=True)), [results[0]]
        )


class EnsureProductRaceTests(StripeRaceTestCase):
    def test_two_concurrent_checkouts_create_one_stripe_product(self) -> None:
        role = baker.make(SubscriptionRole, stripe_product_id="")
        ids = itertools.count(1)
        barrier = threading.Barrier(2, timeout=_PAUSE_SECONDS)
        create = mock.MagicMock(
            side_effect=_held_for_the_other_caller(barrier, lambda: mock.Mock(id=f"prod_{next(ids)}"))
        )

        def ensure() -> str:
            return stripe_client.ensure_product(SubscriptionRole.objects.get(pk=role.pk))

        with mock.patch("stripe.Product.create", create):
            results = run_concurrently([ensure, ensure])

        self.assertEqual(
            create.call_count, 1, "both callers created a Stripe product; the stored id is last-writer-wins"
        )
        self.assertEqual(results[0], results[1])
        role.refresh_from_db()
        self.assertEqual(role.stripe_product_id, results[0])


def _subscription(status: str) -> dict:
    return {
        "id": "sub_race",
        "status": status,
        "cancel_at_period_end": False,
        "canceled_at": None,
        "items": {"data": [{"current_period_end": 1_800_000_000, "price": {"id": "price_1", "unit_amount": 1000}}]},
    }


class UpdatedVersusDeletedRaceTests(StripeRaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.subscription = baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=baker.make(SubscriptionRole),
            stripe_subscription_id="sub_race",
            status=BillingSubscriptionStatus.ACTIVE,
            pledged_amount_cents=1000,
        )

    def test_an_update_in_flight_cannot_undo_a_concurrent_cancellation(self) -> None:
        """The update was sent before the cancellation, so ordering alone does not reject it: only the lock does."""
        update_reading = threading.Event()
        delete_done = threading.Event()
        update_thread: list[int] = []
        delete_landed_mid_update: list[bool] = []
        real = pricing.pledge_meets_threshold

        def pause_the_update(*args: Any, **kwargs: Any) -> bool:
            if threading.get_ident() in update_thread and not update_reading.is_set():
                update_reading.set()
                delete_landed_mid_update.append(delete_done.wait(timeout=_PAUSE_SECONDS))
            return real(*args, **kwargs)

        def update() -> None:
            update_thread.append(threading.get_ident())
            with transaction.atomic():
                webhooks.handle_event(
                    {
                        "type": "customer.subscription.updated",
                        "created": 1_700_000_000,
                        "data": {"object": _subscription("active")},
                    }
                )

        def delete() -> None:
            self.assertTrue(update_reading.wait(timeout=10), "the update never reached the row")
            with transaction.atomic():
                webhooks.handle_event(
                    {
                        "type": "customer.subscription.deleted",
                        "created": 1_700_000_001,
                        "data": {"object": _subscription("canceled")},
                    }
                )
            delete_done.set()

        with mock.patch.object(pricing, "pledge_meets_threshold", side_effect=pause_the_update):
            run_concurrently([update, delete])

        self.assertEqual(
            delete_landed_mid_update, [False], "the cancellation was not held off while the update had the row"
        )
        self.subscription.refresh_from_db()
        self.assertEqual(
            self.subscription.status,
            BillingSubscriptionStatus.CANCELED,
            "a stale update resurrected a canceled subscription",
        )
