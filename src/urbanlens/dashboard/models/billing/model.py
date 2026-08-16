"""Stripe-backed paid subscription models: customers, role subscriptions, and webhook log."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db.models import CASCADE, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField, JSONField, OneToOneField, Q, TextChoices, UniqueConstraint
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.billing.meta import BillingSubscriptionStatus
from urbanlens.dashboard.models.billing.queryset import (
    BillingCustomerManager,
    RoleSubscriptionManager,
    StripeProcessedRefundManager,
    StripeWebhookEventManager,
)


class BillingCustomer(abstract.DashboardModel):
    """Links a user to their Stripe Customer id, created lazily on first checkout."""

    user = OneToOneField(User, on_delete=CASCADE, related_name="billing_customer")
    stripe_customer_id = CharField(max_length=255, unique=True)

    if TYPE_CHECKING:
        user_id: int

    objects = BillingCustomerManager()

    def __str__(self) -> str:
        return f"{self.user} ({self.stripe_customer_id})"


class RoleSubscription(abstract.DashboardModel):
    """A user's paid, Stripe-backed subscription to a SubscriptionRole.

    Distinct from ``UserSubscription`` (an admin-issued grant with no billing behind
    it) - a role can be held either way, and ``active_subscription_roles()``/
    ``user_has_feature()`` check both. ``threshold_met`` only matters for roles with
    ``pwyw_dynamic_threshold`` enabled: it's recomputed against the site's current
    cost-per-user each time Stripe reports a successful charge
    (``services.billing.webhooks``), so a pledge that used to clear the bar can stop
    granting the role's features without the subscription itself changing status.

    Pay-what-you-want roles also accrue a usage ledger (``total_paid_cents``,
    ``amount_used_cents``, ``usage_covered_until`` - see ``services.billing.banking``)
    that can keep ``grants_access`` true independent of ``is_billable``/``threshold_met``,
    for as long as cumulative overpayment covers each elapsed billing period's cost.
    """

    user = ForeignKey(User, on_delete=CASCADE, related_name="role_subscriptions")
    role = ForeignKey("dashboard.SubscriptionRole", on_delete=CASCADE, related_name="paid_subscriptions")

    stripe_subscription_id = CharField(max_length=255, unique=True)
    stripe_price_id = CharField(max_length=255, blank=True)
    pledged_amount_cents = IntegerField(help_text="The amount currently being charged per period, in cents.")
    status = CharField(max_length=20, choices=BillingSubscriptionStatus.choices, default=BillingSubscriptionStatus.INCOMPLETE)
    current_period_end = DateTimeField(null=True, blank=True)
    cancel_at_period_end = BooleanField(default=False)
    canceled_at = DateTimeField(null=True, blank=True)
    threshold_met = BooleanField(default=True, help_text="Whether the current pledge clears the role's pay-what-you-want access threshold.")

    # --- Pay-what-you-want usage ledger (see services.billing.banking) ---

    total_paid_cents = IntegerField(default=0, help_text="Cumulative amount actually paid via Stripe invoices, ever. Pay-what-you-want roles only.")
    amount_used_cents = IntegerField(default=0, help_text="Cumulative reference cost 'spent' from total_paid_cents so far, one billing period at a time.")
    usage_covered_until = DateTimeField(
        null=True,
        blank=True,
        help_text="Point in time through which the usage ledger has confirmed sufficient balance - grants access independent of live Stripe status, even after cancellation, for as long as cumulative overpayment covers it.",
    )

    if TYPE_CHECKING:
        user_id: int
        role_id: int

    objects = RoleSubscriptionManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]
        constraints = [
            UniqueConstraint(
                fields=["user", "role"],
                condition=~Q(status=BillingSubscriptionStatus.CANCELED),
                name="unique_active_role_subscription",
            ),
        ]

    @property
    def pledged_amount_dollars(self) -> Decimal:
        return Decimal(self.pledged_amount_cents) / 100

    @property
    def is_billable(self) -> bool:
        """Whether Stripe is still actively billing this subscription (regardless of threshold_met)."""
        return self.status in (BillingSubscriptionStatus.ACTIVE, BillingSubscriptionStatus.TRIALING)

    @property
    def has_banked_access(self) -> bool:
        """Whether the pay-what-you-want usage ledger currently covers access, independent of Stripe status."""
        return self.usage_covered_until is not None and self.usage_covered_until > timezone.now()

    @property
    def grants_access(self) -> bool:
        return (self.is_billable and self.threshold_met) or self.has_banked_access

    def __str__(self) -> str:
        return f"{self.user} → {self.role} (${self.pledged_amount_dollars}/mo, {self.status})"


class StripeWebhookEvent(abstract.DashboardModel):
    """Idempotency/audit log for incoming Stripe webhook deliveries.

    Stripe redelivers on any non-2xx response or timeout, so every event is recorded
    (with its raw payload, for debugging) before being processed - a replay of an
    already-``processed_at`` event is a no-op, not a re-run of its side effects.
    """

    stripe_event_id = CharField(max_length=255, unique=True)
    event_type = CharField(max_length=100)
    payload = JSONField()
    processed_at = DateTimeField(null=True, blank=True)

    objects = StripeWebhookEventManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.stripe_event_id})"


class StripeProcessedRefund(abstract.DashboardModel):
    """Idempotency record for individual Stripe refund objects already applied.

    ``StripeWebhookEvent`` dedups whole event deliveries, but ``charge.refunded`` is
    cumulative: a second partial refund on the same charge arrives as a *fresh* event
    (new event id) whose ``refunds.data`` re-contains every earlier refund object. Each
    refund id (``re_...``) is claimed here exactly once - within the receiving view's
    transaction, so the claim commits atomically with the ledger decrement it produced.
    """

    stripe_refund_id = CharField(max_length=255, unique=True)
    stripe_charge_id = CharField(max_length=255, blank=True)
    amount_cents = IntegerField(default=0, help_text="The refunded amount applied against the banked balance, in cents.")

    objects = StripeProcessedRefundManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.stripe_refund_id} ({self.amount_cents}c)"
