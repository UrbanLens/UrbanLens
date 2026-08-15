"""Banked pay-what-you-want access: a running usage ledger on RoleSubscription that can
keep granting access after a subscription stops being actively billed.

Kept separate from services.billing.webhooks (dispatch + Stripe-shaped dicts) and
services.billing.pricing (pure role/threshold math with no model mutation) - this is
the one place that turns a payment into ledger state.

The ledger tracks two running totals - ``total_paid_cents`` (everything ever paid) and
``amount_used_cents`` (cost "spent" from that total so far) - and walks forward one
fixed-length billing period at a time, pricing each period at the role's pay-what-you-want
threshold *as of that period's start*, not today's threshold. A period is entered (and its
cost "spent") once its start has been reached and it's affordable
(``total_paid_cents >= amount_used_cents + that period's threshold``); as soon as one isn't
affordable, advancement stops - coverage lands at the end of the last affordable period,
which is typically still ahead of *as_of* (paying for a period covers you through its end,
not just up to the moment you paid). This is deliberately period-by-period rather than a
single ``total_paid / current_threshold`` division: a role's threshold can change over time
(admin edits, or a dynamic cost-per-user that moves month to month), and a single division
would apply today's price to every historical period instead of the price that actually
applied then.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.services.billing import pricing

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.billing import RoleSubscription

#: Matches UserSubscription.set_duration_months's flat-30-day-month convention.
_PERIOD = timedelta(days=30)


def advance_usage_ledger(role_subscription: RoleSubscription, as_of: datetime.datetime | None = None) -> None:
    """Advance a pay-what-you-want subscription's usage ledger as of *as_of*.

    Walks forward from ``usage_covered_until`` (or ``created`` if nothing's been ticked
    yet) one ``_PERIOD`` at a time, pricing each period at the role's pay-what-you-want
    threshold as of that period's start. A period is entered once its start is reached
    (``cursor <= as_of``); stops as soon as a period isn't affordable (``total_paid_cents``
    no longer covers ``amount_used_cents`` plus that period's threshold) or the next
    period hasn't started yet. Coverage lands at the end of the last affordable period -
    which can be, and usually is, later than *as_of* itself. A no-op for non-PWYW roles.

    Args:
        role_subscription: The subscription to advance. Reads role_subscription.role, so
            callers should have it select_related.
        as_of: Point in time to advance as of; defaults to now.
    """
    as_of = as_of or timezone.now()
    cursor = role_subscription.usage_covered_until or role_subscription.created
    amount_used_cents = role_subscription.amount_used_cents
    advanced = False

    while cursor <= as_of:
        threshold = pricing.role_pwyw_threshold_cents(role_subscription.role, as_of=cursor)
        if threshold is None:
            break
        if role_subscription.total_paid_cents < amount_used_cents + threshold:
            break
        amount_used_cents += threshold
        cursor += _PERIOD
        advanced = True

    if not advanced:
        return
    role_subscription.amount_used_cents = amount_used_cents
    role_subscription.usage_covered_until = cursor
    role_subscription.save(update_fields=["amount_used_cents", "usage_covered_until", "updated"])


def apply_payment(role_subscription: RoleSubscription, amount_paid_cents: int, as_of: datetime.datetime | None = None) -> None:
    """Record a successful pay-what-you-want charge and advance its usage ledger.

    A no-op for non-PWYW roles or a non-positive amount.

    Args:
        role_subscription: The subscription to credit. Reads role_subscription.role, so
            callers should have it select_related.
        amount_paid_cents: The invoice's actual amount paid, in cents.
        as_of: Point in time the payment is being processed; defaults to now.
    """
    if not role_subscription.role.pay_what_you_want or amount_paid_cents <= 0:
        return
    role_subscription.total_paid_cents += amount_paid_cents
    role_subscription.save(update_fields=["total_paid_cents", "updated"])
    advance_usage_ledger(role_subscription, as_of)


def apply_refund(role_subscription: RoleSubscription, amount_refunded_cents: int) -> None:
    """Claw back a refunded (or lost-dispute) amount from the banked balance, in full.

    Policy: the refunded amount comes straight out of ``total_paid_cents`` (clamped at
    zero), so future periods simply stop being affordable. Access already consumed is
    forgiven - ``amount_used_cents`` and ``usage_covered_until`` are left untouched, so
    periods the ledger has already entered stay covered through their end.

    A no-op for non-PWYW roles or a non-positive amount.

    Args:
        role_subscription: The subscription to debit. Reads role_subscription.role, so
            callers should have it select_related.
        amount_refunded_cents: The amount refunded or lost to a dispute, in cents.
    """
    if not role_subscription.role.pay_what_you_want or amount_refunded_cents <= 0:
        return
    role_subscription.total_paid_cents = max(0, role_subscription.total_paid_cents - amount_refunded_cents)
    role_subscription.save(update_fields=["total_paid_cents", "updated"])
