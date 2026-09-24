"""Every write of Stripe-owned state onto a RoleSubscription row.

Stripe guarantees neither ordering nor single delivery, and webhooks, checkout completion and the nightly sweep can
all touch one row at once. Each transition here re-reads the row under a lock and applies only if it is not older than
the state already stored (``stripe_state_at``) and does not move the row out of a terminal status. Timestamps are
compared at Stripe's one-second resolution, and a tie applies, since two same-second events cannot be ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone
import stripe

from urbanlens.dashboard.models.billing import TERMINAL_SUBSCRIPTION_STATUSES, BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.services.billing import pricing

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_STRIPE_FIELDS = (
    "status",
    "stripe_price_id",
    "pledged_amount_cents",
    "cancel_at_period_end",
    "current_period_end",
    "canceled_at",
    "threshold_met",
    "stripe_state_at",
)


def stripe_time(unix_timestamp: int | None) -> datetime | None:
    """Convert a Stripe Unix timestamp to an aware datetime, or None when absent."""
    return datetime.fromtimestamp(unix_timestamp, tz=UTC) if unix_timestamp else None


def retrieve_subscription(subscription_id: str) -> tuple[dict, datetime]:
    """Fetch a live Subscription, with the time the request was sent.

    The send time is a lower bound on how fresh the result is, so it orders the result against webhook events.

    Args:
        subscription_id: The Stripe subscription id.

    Returns:
        The Subscription as a plain dict, and when it was requested (whole seconds).
    """
    sent_at = timezone.now().replace(microsecond=0)
    return stripe.Subscription.retrieve(subscription_id).to_dict(), sent_at


def _is_stale(row: RoleSubscription, as_of: datetime | None) -> bool:
    return as_of is not None and row.stripe_state_at is not None and as_of < row.stripe_state_at


def _stamp(row: RoleSubscription, as_of: datetime | None) -> None:
    if as_of is not None and (row.stripe_state_at is None or as_of > row.stripe_state_at):
        row.stripe_state_at = as_of


def _transition(role_subscription: RoleSubscription, apply: Callable[[RoleSubscription], bool]) -> bool:
    """Run *apply* against the locked row, save what it changed, and refresh the caller's copy.

    Args:
        role_subscription: The caller's instance of the row.
        apply: Mutates the locked row; returns False to leave it untouched.

    Returns:
        Whether the transition was applied.
    """
    with transaction.atomic():
        locked = RoleSubscription.objects.locked(role_subscription.pk)
        applied = apply(locked)
        if applied:
            locked.save(update_fields=[*_STRIPE_FIELDS, "updated"])
    for field in _STRIPE_FIELDS:
        setattr(role_subscription, field, getattr(locked, field))
    return applied


def apply_subscription(role_subscription: RoleSubscription, stripe_subscription: dict, as_of: datetime | None) -> bool:
    """Copy status, price and period from a Stripe Subscription snapshot onto the row.

    Writes only the fields Stripe owns, so a pay-what-you-want ledger updated meanwhile is never written back.

    Args:
        role_subscription: The row to update.
        stripe_subscription: The Subscription, as a plain dict.
        as_of: When Stripe produced the snapshot: an event's ``created``, or a retrieve's send time. None skips the
            ordering check.

    Returns:
        Whether the snapshot was applied; False when it is older than the stored state or would reopen a terminal row.
    """
    incoming_status = stripe_subscription["status"]

    def apply(row: RoleSubscription) -> bool:
        if row.is_terminal and incoming_status != row.status:
            logger.info("Ignoring %s snapshot for already-ended subscription %s", incoming_status, row.stripe_subscription_id)
            return False
        if _is_stale(row, as_of):
            logger.info("Ignoring out-of-order snapshot (%s) for subscription %s", as_of, row.stripe_subscription_id)
            return False
        item = stripe_subscription["items"]["data"][0]
        price = item["price"]
        row.status = incoming_status
        row.stripe_price_id = price["id"]
        row.pledged_amount_cents = price["unit_amount"]
        row.cancel_at_period_end = bool(stripe_subscription.get("cancel_at_period_end"))
        row.current_period_end = stripe_time(item.get("current_period_end"))
        row.canceled_at = stripe_time(stripe_subscription.get("canceled_at"))
        row.threshold_met = pricing.pledge_meets_threshold(row.role, row.pledged_amount_cents)
        _stamp(row, as_of)
        return True

    return _transition(role_subscription, apply)


def mark_canceled(role_subscription: RoleSubscription, canceled_at: datetime | None, as_of: datetime | None) -> None:
    """Record that Stripe canceled the subscription. Cancellation is final, so it applies whatever its timestamp.

    Args:
        role_subscription: The row to cancel.
        canceled_at: Stripe's cancellation time; defaults to now.
        as_of: The event's ``created`` time.
    """

    def apply(row: RoleSubscription) -> bool:
        row.status = BillingSubscriptionStatus.CANCELED
        row.canceled_at = canceled_at or row.canceled_at or timezone.now()
        _stamp(row, as_of)
        return True

    _transition(role_subscription, apply)


def mark_past_due(role_subscription: RoleSubscription, as_of: datetime | None) -> bool:
    """Record a failed renewal payment, unless newer state or a terminal status supersedes it.

    Args:
        role_subscription: The row to update.
        as_of: The event's ``created`` time.

    Returns:
        Whether the row was marked past due.
    """

    def apply(row: RoleSubscription) -> bool:
        if row.status in TERMINAL_SUBSCRIPTION_STATUSES or _is_stale(row, as_of):
            return False
        row.status = BillingSubscriptionStatus.PAST_DUE
        _stamp(row, as_of)
        return True

    return _transition(role_subscription, apply)
