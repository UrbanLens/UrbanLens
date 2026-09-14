"""Two simultaneous deliveries of one Stripe event must credit the payment once (P57).

`StripeWebhookView.post` re-reads the event row under `select_for_update` before handling it. The sequential tests in
`test_billing_webhook_idempotency.py` run on one connection, where that lock never contends, so a dropped lock passes
them. Here each delivery runs on its own thread and connection, and the handler holds until the other delivery arrives,
so both reach it together unless the lock keeps the second one out.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import json
import threading
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription, StripeWebhookEvent
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import webhooks as billing_webhooks
from urbanlens.UrbanLens.settings.app import settings as app_settings


def _stripe_subscription() -> dict:
    return {
        "id": "sub_test",
        "status": "active",
        "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": "price_test", "unit_amount": 1000}, "current_period_end": 1_800_000_000}]},
    }


def _event(event_id: str, amount_paid: int) -> dict:
    return {
        "id": event_id,
        "type": "invoice.payment_succeeded",
        "data": {"object": {"subscription": "sub_test", "amount_paid": amount_paid}},
    }


def _held_until_both_arrive(barrier: threading.Barrier, real: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``real`` so each caller waits for the other first, or gives up after the barrier's timeout."""

    def held(*args: Any, **kwargs: Any) -> Any:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return real(*args, **kwargs)

    return held


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class WebhookEventLockTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.subscription = baker.make(
            RoleSubscription,
            user=baker.make(User),
            role=role,
            stripe_subscription_id="sub_test",
            status=BillingSubscriptionStatus.ACTIVE,
            total_paid_cents=0,
        )

    def _deliver(self, event: dict) -> Callable[[], int]:
        def post() -> int:
            response = Client().post(
                reverse("billing.stripe_webhook"),
                data=json.dumps(event),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=stub",
            )
            return response.status_code

        return post

    def _deliver_concurrently(self, first: dict, second: dict) -> list[int]:
        barrier = threading.Barrier(2, timeout=2)
        with (
            mock.patch.object(app_settings, "stripe_webhook_secret", "whsec_test"),
            mock.patch(
                "stripe.Webhook.construct_event",
                side_effect=lambda body, _sig, _secret: mock.Mock(to_dict=lambda: json.loads(body)),
            ),
            mock.patch("stripe.Subscription.retrieve", return_value=mock.Mock(to_dict=_stripe_subscription)),
            mock.patch.object(
                billing_webhooks,
                "handle_event",
                side_effect=_held_until_both_arrive(barrier, billing_webhooks.handle_event),
            ),
        ):
            return run_concurrently([self._deliver(first), self._deliver(second)])

    def test_two_simultaneous_deliveries_of_one_event_credit_it_once(self) -> None:
        event = _event("evt_same", 1000)

        statuses = self._deliver_concurrently(event, event)

        self.assertEqual(statuses, [200, 200])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 1000, "both deliveries ran the handler")
        self.assertIsNotNone(StripeWebhookEvent.objects.get(stripe_event_id="evt_same").processed_at)

    def test_two_simultaneous_distinct_events_are_both_credited(self) -> None:
        """The handler is reached by both threads at once when nothing should stop them, so the test above is not
        passing because the second delivery never got there."""
        statuses = self._deliver_concurrently(_event("evt_a", 400), _event("evt_b", 700))

        self.assertEqual(statuses, [200, 200])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.total_paid_cents, 1100)
