"""Stripe-backed paid subscription models: customers, role subscriptions, and webhook log."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db.models import CASCADE, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField, JSONField, OneToOneField, Q, TextChoices, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.billing.queryset import BillingCustomerManager, RoleSubscriptionManager, StripeWebhookEventManager


class BillingCustomer(abstract.DashboardModel):
    """Links a user to their Stripe Customer id, created lazily on first checkout."""

    user = OneToOneField(User, on_delete=CASCADE, related_name="billing_customer")
    stripe_customer_id = CharField(max_length=255, unique=True)

    if TYPE_CHECKING:
        user_id: int

    objects = BillingCustomerManager()

    def __str__(self) -> str:
        return f"{self.user} ({self.stripe_customer_id})"


class BillingSubscriptionStatus(TextChoices):
    """Mirrors Stripe's own ``Subscription.status`` values."""

    INCOMPLETE = "incomplete", "Incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired", "Incomplete (expired)"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past due"
    CANCELED = "canceled", "Canceled"
    UNPAID = "unpaid", "Unpaid"
    TRIALING = "trialing", "Trialing"
    PAUSED = "paused", "Paused"


class RoleSubscription(abstract.DashboardModel):
    """A user's paid, Stripe-backed subscription to a SubscriptionRole.

    Distinct from ``UserSubscription`` (an admin-issued grant with no billing behind
    it) - a role can be held either way, and ``active_subscription_roles()``/
    ``user_has_feature()`` check both. ``threshold_met`` only matters for roles with
    ``pwyw_dynamic_threshold`` enabled: it's recomputed against the site's current
    cost-per-user each time Stripe reports a successful charge
    (``services.billing.webhooks``), so a pledge that used to clear the bar can stop
    granting the role's features without the subscription itself changing status.
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
    def grants_access(self) -> bool:
        return self.is_billable and self.threshold_met

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
