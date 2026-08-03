"""BillingCustomer, RoleSubscription, and StripeWebhookEvent querysets and managers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.billing.model import BillingCustomer, RoleSubscription, StripeWebhookEvent


class BillingCustomerQuerySet(abstract.DashboardQuerySet["BillingCustomer"]):
    """Filters for BillingCustomer."""


class BillingCustomerManager(abstract.DashboardManager.from_queryset(BillingCustomerQuerySet)):
    pass


class RoleSubscriptionQuerySet(abstract.DashboardQuerySet["RoleSubscription"]):
    """Filters for paid, Stripe-backed role subscriptions."""

    def granting_access_for(self, user: User) -> Self:
        """Subscriptions currently entitling *user* to their role's features.

        Either an active/trialing Stripe status with a cleared pay-what-you-want
        threshold, or unexpired banked usage-ledger coverage - the latter can apply to
        a row in ANY status (canceled, past_due, etc.), since banked access is meant to
        survive exactly the "stopped paying" case (see services.billing.banking).

        Args:
            user: The user to look up.

        Returns:
            Matching subscriptions.
        """
        from django.utils import timezone

        from urbanlens.dashboard.models.billing.model import BillingSubscriptionStatus

        return self.filter(user=user).filter(
            Q(status__in=(BillingSubscriptionStatus.ACTIVE, BillingSubscriptionStatus.TRIALING), threshold_met=True)
            | Q(usage_covered_until__gt=timezone.now())
        )

    def visible_for(self, user: User) -> Self:
        """Rows worth surfacing in Settings > Billing: anything not canceled, plus a
        canceled row that's still granting access via unexpired banked usage-ledger
        coverage (so a user can see how much paid-ahead runway remains after canceling).

        Args:
            user: The user to look up.

        Returns:
            Matching subscriptions.
        """
        from django.utils import timezone

        from urbanlens.dashboard.models.billing.model import BillingSubscriptionStatus

        return self.filter(user=user).filter(~Q(status=BillingSubscriptionStatus.CANCELED) | Q(usage_covered_until__gt=timezone.now()))

    def not_canceled(self) -> Self:
        """Subscriptions that haven't reached a terminal canceled state."""
        from urbanlens.dashboard.models.billing.model import BillingSubscriptionStatus

        return self.exclude(status=BillingSubscriptionStatus.CANCELED)

    def for_stripe_subscription(self, stripe_subscription_id: str) -> RoleSubscription | None:
        """Return the row for a given Stripe subscription id, or None.

        Args:
            stripe_subscription_id: The Stripe ``Subscription`` id (``sub_...``).

        Returns:
            The matching RoleSubscription, or None.
        """
        return self.filter(stripe_subscription_id=stripe_subscription_id).select_related("role", "user").first()


class RoleSubscriptionManager(abstract.DashboardManager.from_queryset(RoleSubscriptionQuerySet)):
    pass


class StripeWebhookEventQuerySet(abstract.DashboardQuerySet["StripeWebhookEvent"]):
    """Filters for the Stripe webhook idempotency/audit log."""

    def unprocessed(self) -> Self:
        """Events stored but not yet successfully handled."""
        return self.filter(processed_at__isnull=True)


class StripeWebhookEventManager(abstract.DashboardManager.from_queryset(StripeWebhookEventQuerySet)):
    pass
