"""A Stripe sync must write only the fields Stripe is authoritative for.

``sync_from_stripe_subscription`` copies status/price/period off a live Stripe
Subscription and then calls a bare ``save()`` - which writes *every* column on
the instance, including the three the pay-what-you-want ledger owns
(``total_paid_cents``, ``amount_used_cents``, ``usage_covered_until``). Stripe
has no opinion about those; they are computed locally from payments.

That turns every sync into a blind write of ledger state from whatever snapshot
the instance happens to hold, and both callers hold theirs across a network
round-trip:

- ``_handle_invoice_payment_succeeded`` loads the subscription, calls
  ``stripe.Subscription.retrieve``, then syncs - so anything that advanced the
  ledger during that round-trip is written back to its pre-call value.
- ``sync_stripe_subscriptions``, the nightly sweep, does a ``retrieve`` per row
  over every non-canceled subscription, holding each snapshot for the length of
  its own API call.

Locking the row inside ``banking`` does not help here: this is a different
function, it writes the ledger columns without going through ``banking`` at all,
and in the payment handler it runs *before* ``apply_payment`` - so the locked,
refreshed read that follows picks up the stale values this just committed.

The interleaving is modelled with two snapshots of one row rather than threads,
for the reasons given in ``test_billing_ledger_concurrency``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import banking, webhooks


def _payload(status: str = "active", unit_amount: int = 1000, canceled_at: int | None = None) -> dict:
    """The shape ``sync_from_stripe_subscription`` reads."""
    return {
        "id": "sub_test",
        "status": status,
        "cancel_at_period_end": False,
        "canceled_at": canceled_at,
        "items": {"data": [{"price": {"id": "price_test", "unit_amount": unit_amount}, "current_period_end": 1_800_000_000}]},
    }


class SyncFieldScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500)
        self.sub = baker.make(RoleSubscription, user=self.user, role=self.role, stripe_subscription_id="sub_test")
        self.start = timezone.now() - timedelta(days=200)
        RoleSubscription.objects.filter(pk=self.sub.pk).update(created=self.start)
        self.sub.refresh_from_db()

    def _snapshot(self) -> RoleSubscription:
        return RoleSubscription.objects.select_related("role").get(pk=self.sub.pk)

    def test_a_sync_does_not_erase_a_payment_it_never_saw(self) -> None:
        """The snapshot predates the payment; syncing must not write its ledger back."""
        stale = self._snapshot()

        banking.apply_payment(self._snapshot(), 5000, as_of=self.start)
        paid = RoleSubscription.objects.get(pk=self.sub.pk)

        webhooks.sync_from_stripe_subscription(stale, _payload())

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.total_paid_cents, 5000, "a Stripe sync erased a payment's credit")
        self.assertEqual(self.sub.amount_used_cents, paid.amount_used_cents, "a Stripe sync rewound the usage ledger")
        self.assertEqual(self.sub.usage_covered_until, paid.usage_covered_until, "a Stripe sync rewound paid-for coverage")

    def test_the_sync_still_applies_every_field_stripe_owns(self) -> None:
        """The complement: narrowing what it writes must not stop it writing."""
        subscription = self._snapshot()

        webhooks.sync_from_stripe_subscription(subscription, _payload(status="past_due", unit_amount=2500, canceled_at=1_700_000_000))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "past_due")
        self.assertEqual(self.sub.stripe_price_id, "price_test")
        self.assertEqual(self.sub.pledged_amount_cents, 2500)
        self.assertFalse(self.sub.cancel_at_period_end)
        self.assertIsNotNone(self.sub.current_period_end)
        self.assertTrue(self.sub.threshold_met)
        # canceled_at is in the same update_fields list as the ledger columns this file
        # exists to keep untouched - a narrowing mistake could drop it unnoticed.
        self.assertEqual(self.sub.canceled_at, datetime.fromtimestamp(1_700_000_000, tz=UTC))

    def test_a_pledge_under_the_threshold_still_clears_threshold_met(self) -> None:
        """threshold_met is recomputed, not merely set - it has to fall as well as rise."""
        RoleSubscription.objects.filter(pk=self.sub.pk).update(threshold_met=True)

        webhooks.sync_from_stripe_subscription(self._snapshot(), _payload(unit_amount=100))

        self.sub.refresh_from_db()
        self.assertFalse(self.sub.threshold_met)
