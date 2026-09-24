"""Handlers for verified Stripe webhook events.

Every handler resolves its target row and no-ops (logs + returns) if it isn't found yet rather than raising, since
Stripe delivers events with no ordering guarantee. Handlers take the event's ``created`` time so that
``subscription_state`` can discard state older than what a row already holds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
import stripe

from urbanlens.dashboard.models.billing import TERMINAL_SUBSCRIPTION_STATUSES
from urbanlens.dashboard.services.billing import banking, stripe_client, subscription_state
from urbanlens.dashboard.services.billing.subscription_state import retrieve_subscription, stripe_time
from urbanlens.dashboard.services.core.numbers import safe_int_or_none

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

logger = logging.getLogger(__name__)

#: Refunds in these states returned nothing to the customer.
_UNPAID_REFUND_STATUSES = frozenset({"failed", "canceled"})


def _id_of(value: object) -> str | None:
    """An expandable Stripe field's id, whether it arrived as an id or an expanded object."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("id") or None
    return None


def _invoice_subscription_id(invoice: dict) -> str | None:
    """The subscription an invoice bills, from either API shape.

    Since API version 2025-03-31 it lives at ``parent.subscription_details.subscription``. Webhook payloads follow the
    endpoint's configured version, so the older top-level ``subscription`` is still read.
    """
    legacy = _id_of(invoice.get("subscription"))
    if legacy:
        return legacy
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return _id_of(details.get("subscription"))


def _charge_subscription_id(charge: dict) -> str | None:
    """The subscription a charge paid for, via its invoice.

    Current API versions drop ``Charge.invoice``; the link runs through the InvoicePayment for the charge's
    PaymentIntent instead.
    """
    invoice_id = _id_of(charge.get("invoice"))
    if not invoice_id:
        payment_intent_id = _id_of(charge.get("payment_intent"))
        if not payment_intent_id:
            return None
        payments = stripe.InvoicePayment.list(payment={"type": "payment_intent", "payment_intent": payment_intent_id}, limit=1)
        invoice_id = next((_id_of(payment.to_dict().get("invoice")) for payment in payments.data), None)
        if not invoice_id:
            return None
    return _invoice_subscription_id(stripe.Invoice.retrieve(invoice_id).to_dict())


def _cancel_duplicate(subscription_id: str, stripe_subscription: dict, live: RoleSubscription) -> None:
    """Stop Stripe billing a second live subscription to a role the user already holds.

    The one-live-row constraint means it could never be recorded, and nothing else would stop its renewals.
    """
    logger.error(
        "Subscription %s duplicates live subscription %s (user %s, role %s); canceling it. Its first payment needs a manual refund review.",
        subscription_id,
        live.stripe_subscription_id,
        live.user_id,
        live.role_id,
    )
    if stripe_subscription.get("status") in TERMINAL_SUBSCRIPTION_STATUSES:
        return
    stripe.Subscription.cancel(subscription_id, idempotency_key=stripe_client.idempotency_key("cancel-duplicate", subscription_id))


def _get_or_create_role_subscription(user: User, role: SubscriptionRole, subscription_id: str, stripe_subscription: dict, as_of: datetime | None) -> RoleSubscription | None:
    """Create (or fetch) the RoleSubscription row for a Stripe subscription id.

    Runs under the user's billing lock, so first sightings of two new subscriptions for one user are serialised.

    Args:
        user: The subscribing user.
        role: The role being subscribed to.
        subscription_id: The Stripe subscription id.
        stripe_subscription: The live Subscription, as a plain dict.
        as_of: When ``stripe_subscription`` was requested.

    Returns:
        The existing or newly created RoleSubscription, synced from ``stripe_subscription``; None when it duplicates
        another live subscription to the same role and was canceled instead.
    """
    from urbanlens.dashboard.models.billing import RoleSubscription

    with transaction.atomic():
        stripe_client.lock_billing_owner(user)
        existing = RoleSubscription.objects.filter(stripe_subscription_id=subscription_id).select_related("role").first()
        if existing is not None:
            subscription_state.apply_subscription(existing, stripe_subscription, as_of)
            return existing

        for live in RoleSubscription.objects.not_terminal().filter(user=user, role=role).select_related("role"):
            # A missed `deleted` webhook leaves a row looking live; ask Stripe before calling the new one a duplicate.
            live_state, live_as_of = retrieve_subscription(live.stripe_subscription_id)
            subscription_state.apply_subscription(live, live_state, live_as_of)
            if not live.is_terminal:
                _cancel_duplicate(subscription_id, stripe_subscription, live)
                return None

        defaults = {"user": user, "role": role, "pledged_amount_cents": 0}
        if role.pay_what_you_want:
            # Carry the banked balance across a cancel-and-resubscribe rather than starting the ledger from zero.
            previous = RoleSubscription.objects.filter(user=user, role=role).order_by("-created").first()
            if previous is not None:
                defaults["total_paid_cents"] = previous.total_paid_cents
                defaults["amount_used_cents"] = previous.amount_used_cents
                defaults["usage_covered_until"] = previous.usage_covered_until
        try:
            with transaction.atomic():
                role_subscription = RoleSubscription.objects.create(stripe_subscription_id=subscription_id, **defaults)
        except IntegrityError:
            role_subscription = RoleSubscription.objects.select_related("role").get(stripe_subscription_id=subscription_id)
        subscription_state.apply_subscription(role_subscription, stripe_subscription, as_of)
        return role_subscription


def _recover_role_subscription_from_metadata(subscription_id: str) -> RoleSubscription | None:
    """Create the RoleSubscription row for *subscription_id* from Stripe's own metadata.

    For when a webhook that only ever carries a bare subscription id (never a Checkout Session) is delivered before
    ``checkout.session.completed`` has created the row - Stripe guarantees neither webhook ordering nor single delivery.

    Args:
        subscription_id: The Stripe subscription id to create a row for.

    Returns:
        The RoleSubscription, or None when its metadata can't be resolved to a real user and role, or it duplicates a
        live subscription.
    """
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

    stripe_subscription, as_of = retrieve_subscription(subscription_id)
    metadata = stripe_subscription.get("metadata") or {}
    user = User.objects.filter(pk=safe_int_or_none(metadata.get("user_id"))).first()
    role = SubscriptionRole.objects.filter(pk=safe_int_or_none(metadata.get("role_id"))).first()
    if user is None or role is None:
        logger.error(
            "Cannot recover subscription %s: user_id=%s role_id=%s did not resolve",
            subscription_id,
            metadata.get("user_id"),
            metadata.get("role_id"),
        )
        return None
    return _get_or_create_role_subscription(user, role, subscription_id, stripe_subscription, as_of)


def _handle_checkout_session_completed(session: dict, _occurred_at: datetime | None) -> None:
    if session.get("mode") != "subscription":
        return

    subscription_id = session.get("subscription")
    user_id = session.get("client_reference_id")
    if not subscription_id or not user_id:
        logger.warning("checkout.session.completed %s missing subscription/client_reference_id", session.get("id"))
        return

    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.billing import BillingCustomer
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        logger.error("checkout.session.completed %s: no user with id %s", session.get("id"), user_id)
        return

    customer_id = session.get("customer")
    if customer_id:
        with transaction.atomic():
            stripe_client.lock_billing_owner(user)
            BillingCustomer.objects.get_or_create(user=user, defaults={"stripe_customer_id": customer_id})

    role_id = (session.get("metadata") or {}).get("role_id")
    role = SubscriptionRole.objects.filter(pk=role_id).first() if role_id else None
    if role is None:
        logger.error("checkout.session.completed %s: could not resolve role from metadata", session.get("id"))
        return

    stripe_subscription, as_of = retrieve_subscription(subscription_id)
    _get_or_create_role_subscription(user, role, subscription_id, stripe_subscription, as_of)


def _handle_subscription_updated(stripe_subscription: dict, occurred_at: datetime | None) -> None:
    from urbanlens.dashboard.models.billing import RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(stripe_subscription["id"])
    if role_subscription is None:
        logger.info("customer.subscription.updated for unknown subscription %s", stripe_subscription.get("id"))
        return
    subscription_state.apply_subscription(role_subscription, stripe_subscription, occurred_at)


def _handle_subscription_deleted(stripe_subscription: dict, occurred_at: datetime | None) -> None:
    from urbanlens.dashboard.models.billing import RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(stripe_subscription["id"])
    if role_subscription is None:
        return
    subscription_state.mark_canceled(role_subscription, stripe_time(stripe_subscription.get("canceled_at")), occurred_at)


def _handle_invoice_payment_succeeded(invoice: dict, _occurred_at: datetime | None) -> None:
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return

    from urbanlens.dashboard.models.billing import RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        # Can be a genuine race with checkout.session.completed, and Stripe never retries a delivered webhook, so
        # recovering the row from the Subscription's own metadata is the only chance to bank this payment.
        role_subscription = _recover_role_subscription_from_metadata(subscription_id)
        if role_subscription is None:
            return
    else:
        # The live Subscription, not the invoice, says whether this charge cleared a dynamic threshold.
        stripe_subscription, as_of = retrieve_subscription(subscription_id)
        subscription_state.apply_subscription(role_subscription, stripe_subscription, as_of)
    banking.apply_payment(role_subscription, invoice.get("amount_paid") or 0)


def _handle_invoice_payment_failed(invoice: dict, occurred_at: datetime | None) -> None:
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return

    from urbanlens.dashboard.models.billing import RoleSubscription

    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        return
    subscription_state.mark_past_due(role_subscription, occurred_at)


def _handle_charge_refunded(charge: dict, _occurred_at: datetime | None) -> None:
    """Debit each newly seen refund on *charge* from the banked pay-what-you-want balance."""
    from urbanlens.dashboard.models.billing import RoleSubscription, StripeProcessedRefund

    subscription_id = _charge_subscription_id(charge)
    if not subscription_id:
        # A one-off charge, not a subscription invoice - nothing was ever banked from it.
        return
    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        logger.info("charge.refunded for unknown subscription %s", subscription_id)
        return

    embedded = charge.get("refunds")
    if embedded and not embedded.get("has_more"):
        refunds = embedded.get("data") or []
    else:
        # Current API versions omit `refunds` from charges, and an embedded page stops at 10.
        refunds = [r.to_dict() for r in stripe.Refund.list(charge=charge.get("id") or "", limit=100).auto_paging_iter()]
    if not refunds:
        logger.warning("charge.refunded %s listed no refunds; nothing applied", charge.get("id"))
        return

    for refund in refunds:
        refund_id = refund.get("id")
        if not refund_id or refund.get("status") in _UNPAID_REFUND_STATUSES:
            continue
        amount = refund.get("amount") or 0
        _record, created = StripeProcessedRefund.objects.get_or_create(
            stripe_refund_id=refund_id,
            defaults={"stripe_charge_id": charge.get("id") or "", "amount_cents": amount},
        )
        if created:
            banking.apply_refund(role_subscription, amount)


def _handle_charge_dispute_closed(dispute: dict, _occurred_at: datetime | None) -> None:
    """Debit a lost dispute's amount from the banked pay-what-you-want balance.

    Only a dispute closing as ``lost`` moves money (Stripe has taken it back); every other closing status leaves the
    ledger alone.
    """
    if dispute.get("status") != "lost":
        return
    charge_id = _id_of(dispute.get("charge"))
    if not charge_id:
        return

    from urbanlens.dashboard.models.billing import RoleSubscription

    subscription_id = _charge_subscription_id(stripe.Charge.retrieve(charge_id).to_dict())
    if not subscription_id:
        return
    role_subscription = RoleSubscription.objects.for_stripe_subscription(subscription_id)
    if role_subscription is None:
        logger.info("charge.dispute.closed for unknown subscription %s", subscription_id)
        return
    banking.apply_refund(role_subscription, dispute.get("amount") or 0)


_HANDLERS: dict[str, Callable[[dict, datetime | None], None]] = {
    "checkout.session.completed": _handle_checkout_session_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_succeeded": _handle_invoice_payment_succeeded,
    "invoice.payment_failed": _handle_invoice_payment_failed,
    "charge.refunded": _handle_charge_refunded,
    "charge.dispute.closed": _handle_charge_dispute_closed,
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
    handler(event["data"]["object"], stripe_time(event.get("created")))
