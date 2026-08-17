"""Handlers for verified Stripe webhook events.

Called from ``controllers.billing_webhooks.StripeWebhookView`` after signature
verification and idempotency dedup - everything here assumes the event is genuine and
not a replay. Every handler resolves its target row and no-ops (logs + returns) if it
isn't found yet rather than raising, since Stripe delivers events with no ordering
guarantee (e.g. ``customer.subscription.updated`` can arrive before
``checkout.session.completed`` has created the row it would update).
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

import stripe

from urbanlens.dashboard.services.billing import banking, pricing

if TYPE_CHECKING:
    from urbanlens.dashboard.models.billing import RoleSubscription

logger = logging.getLogger(__name__)


def _to_datetime(unix_timestamp: int | None) -> datetime | None:
    return datetime.fromtimestamp(unix_timestamp, tz=UTC) if unix_timestamp else None


def sync_from_stripe_subscription(role_subscription: RoleSubscription, stripe_subscription: dict) -> None:
    """Copy status/price/period fields from a live Stripe Subscription onto *role_subscription*.

    Also recomputes ``threshold_met`` against the role's *current* pay-what-you-want
    threshold - this is the actual "did this charge clear the bar" moment for
    dynamic-threshold roles, since ``cost_per_user()`` can drift between billing cycles.

    Saved with an explicit field list, not a bare ``save()``. Stripe is authoritative
    for exactly these columns; the usage ledger's (``total_paid_cents``,
    ``amount_used_cents``, ``usage_covered_until``) are computed locally from payments,
    and writing them from this instance means writing whichever values it was loaded
    with. Every caller holds that instance across a ``Subscription.retrieve``
    round-trip, so a payment landing in that window was being written back out.
    """
    item = stripe_subscription["items"]["data"][0]
    price = item["price"]

    role_subscription.status = stripe_subscription["status"]
    role_subscription.stripe_price_id = price["id"]
    role_subscription.pledged_amount_cents = price["unit_amount"]
    role_subscription.cancel_at_period_end = bool(stripe_subscription.get("cancel_at_period_end"))
    role_subscription.current_period_end = _to_datetime(item.get("current_period_end"))
    role_subscription.canceled_at = _to_datetime(stripe_subscription.get("canceled_at"))
    role_subscription.threshold_met = pricing.pledge_meets_threshold(role_subscription.role, role_subscription.pledged_amount_cents)
    role_subscription.save(
        update_fields=[
            "status",
            "stripe_price_id",
            "pledged_amount_cents",
            "cancel_at_period_end",
            "current_period_end",
            "canceled_at",
            "threshold_met",
            "updated",
        ]
    )


def _handle_checkout_session_completed(session: dict) -> None:
    if session.get("mode") != "subscription":
        return

    subscription_id = session.get("subscription")
    user_id = session.get("client_reference_id")
    if not subscription_id or not user_id:
        logger.warning("checkout.session.completed %s missing subscription/client_reference_id", session.get("id"))
        return

    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.billing import BillingCustomer, RoleSubscription
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        logger.error("checkout.session.completed %s: no user with id %s", session.get("id"), user_id)
        return

    customer_id = session.get("customer")
    if customer_id:
        BillingCustomer.objects.get_or_create(user=user, defaults={"stripe_customer_id": customer_id})

    role_id = (session.get("metadata") or {}).get("role_id")
    role = SubscriptionRole.objects.filter(pk=role_id).first() if role_id else None
    if role is None:
        logger.error("checkout.session.completed %s: could not resolve role from metadata", session.get("id"))
        return

    stripe_subscription = stripe.Subscription.retrieve(subscription_id).to_dict()
    defaults = {"user": user, "role": role, "pledged_amount_cents": 0}
    if role.pay_what_you_want:
        # Multiple RoleSubscription rows can exist per (user, role) over time (the
        # uniqueness constraint only applies to non-canceled rows), so a resubscribe
        # after canceling would otherwise start a fresh usage ledger from zero and
        # orphan any banked balance the prior row had earned.
        previous = RoleSubscription.objects.filter(user=user, role=role).order_by("-created").first()
        if previous is not None:
            defaults["total_paid_cents"] = previous.total_paid_cents
            defaults["amount_used_cents"] = previous.amount_used_cents
            defaults["usage_covered_until"] = previous.usage_covered_until

    role_subscription, _created = RoleSubscription.objects.get_or_create(
        stripe_subscription_id=subscription_id,
        defaults=defaults,
    )
    sync_from_stripe_subscription(role_subscription, stripe_subscription)


def _handle_subscription_updated(stripe_subscription: dict) -> None:
    from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(stripe_subscription["id"])
    if role_subscription is None:
        logger.info("customer.subscription.updated for unknown subscription %s", stripe_subscription.get("id"))
        return

    # Stripe guarantees neither ordering nor single delivery, and emits `updated`
    # alongside `deleted` at cancellation - so a retried `updated` carrying the
    # pre-cancellation status can arrive after the `deleted` that closed this
    # subscription. Applying it verbatim would hand back whatever access the role
    # grants until the nightly reconciliation sweep undid it.
    #
    # Cancellation is terminal at Stripe: a canceled subscription is never
    # reactivated, a new one is created instead. So a later payload that claims
    # otherwise is always the stale one, and dropping it loses nothing.
    #
    # Not to be confused with a subscription set to cancel at period end, which
    # *is* reversible - but that one's status stays `active` until the period
    # closes, so it never reaches this branch.
    if role_subscription.status == BillingSubscriptionStatus.CANCELED and stripe_subscription.get("status") != BillingSubscriptionStatus.CANCELED:
        logger.info(
            "Ignoring out-of-order customer.subscription.updated (status=%s) for already-canceled subscription %s",
            stripe_subscription.get("status"),
            stripe_subscription.get("id"),
        )
        return

    sync_from_stripe_subscription(role_subscription, stripe_subscription)


def _handle_subscription_deleted(stripe_subscription: dict) -> None:
    from django.utils import timezone

    from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(stripe_subscription["id"])
    if role_subscription is None:
        return
    role_subscription.status = BillingSubscriptionStatus.CANCELED
    role_subscription.canceled_at = _to_datetime(stripe_subscription.get("canceled_at")) or timezone.now()
    role_subscription.save(update_fields=["status", "canceled_at", "updated"])


def _handle_invoice_payment_succeeded(invoice: dict) -> None:
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    from urbanlens.dashboard.models.billing import RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        logger.info("invoice.payment_succeeded for unknown subscription %s", subscription_id)
        return
    # Refetched rather than read off the invoice: this is the "did this charge clear the
    # bar" moment for dynamic-threshold roles, and the live Subscription is the single
    # source of truth for status/price/period that sync_from_stripe_subscription expects.
    stripe_subscription = stripe.Subscription.retrieve(subscription_id).to_dict()
    sync_from_stripe_subscription(role_subscription, stripe_subscription)
    banking.apply_payment(role_subscription, invoice.get("amount_paid") or 0)


def _handle_invoice_payment_failed(invoice: dict) -> None:
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        return
    role_subscription.status = BillingSubscriptionStatus.PAST_DUE
    role_subscription.save(update_fields=["status", "updated"])


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_session_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_succeeded": _handle_invoice_payment_succeeded,
    "invoice.payment_failed": _handle_invoice_payment_failed,
}


def handle_event(event: dict) -> None:
    """Dispatch a verified Stripe event to its handler, if one is registered.

    Args:
        event: The full Stripe event payload (already signature-verified).
    """
    handler = _HANDLERS.get(event.get("type", ""))
    if handler is None:
        logger.debug("Ignoring unhandled Stripe event type: %s", event.get("type"))
        return
    handler(event["data"]["object"])
