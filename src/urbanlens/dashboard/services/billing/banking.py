"""Banked pay-what-you-want access: a running usage ledger on RoleSubscription that can keep granting access after a subscription stops being actively billed."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.services.billing import pricing

if TYPE_CHECKING:
    from collections.abc import Iterator

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.billing import RoleSubscription

#: Matches UserSubscription.set_duration_months's flat-30-day-month convention.
_PERIOD = timedelta(days=30)


@contextmanager
def _locked(role_subscription: RoleSubscription) -> Iterator[RoleSubscription]:
    """Hold a row lock on this subscription for the body of the block.

    Args:
        role_subscription: The subscription to lock.

    Yields:
        The freshly-read, locked subscription."""
    with transaction.atomic():
        locked = type(role_subscription).objects.select_for_update().get(pk=role_subscription.pk)
        yield locked
    for field in ("total_paid_cents", "amount_used_cents", "usage_covered_until"):
        setattr(role_subscription, field, getattr(locked, field))


def advance_usage_ledger(role_subscription: RoleSubscription, as_of: datetime.datetime | None = None) -> None:
    """Advance a pay-what-you-want subscription's usage ledger as of *as_of*.

    Args:
        role_subscription: The subscription to advance.
        as_of: Point in time to advance as of; defaults to now."""
    as_of = as_of or timezone.now()
    # Locked here, not only by callers: ``advance_pwyw_usage_ledgers`` sweeps every
    # pay-what-you-want subscription and calls this directly, holding each row's snapshot for as
    # long as its loop takes to reach that row.
    # Re-locking inside apply_payment's block costs one redundant SELECT, since the row is held
    with _locked(role_subscription) as locked:
        # Carry the caller's already-loaded role across: the lock re-reads the row
        # without select_related, so every pricing lookup below would refetch it.
        locked.role = role_subscription.role
        cursor = locked.usage_covered_until or locked.created
        amount_used_cents = locked.amount_used_cents
        advanced = False

        while cursor <= as_of:
            threshold = pricing.role_pwyw_threshold_cents(locked.role, as_of=cursor)
            if threshold is None:
                break
            if locked.total_paid_cents < amount_used_cents + threshold:
                break
            amount_used_cents += threshold
            cursor += _PERIOD
            advanced = True

        if not advanced:
            return
        locked.amount_used_cents = amount_used_cents
        locked.usage_covered_until = cursor
        locked.save(update_fields=["amount_used_cents", "usage_covered_until", "updated"])


def apply_payment(role_subscription: RoleSubscription, amount_paid_cents: int, as_of: datetime.datetime | None = None) -> None:
    """Record a successful pay-what-you-want charge and advance its usage ledger.

    Args:
        role_subscription: The subscription to credit.
        amount_paid_cents: The invoice's actual amount paid, in cents.
        as_of: Point in time the payment is being processed; defaults to now."""
    if not role_subscription.role.pay_what_you_want or amount_paid_cents <= 0:
        return
    with _locked(role_subscription) as locked:
        locked.total_paid_cents += amount_paid_cents
        locked.save(update_fields=["total_paid_cents", "updated"])
        # Inside the lock: advancing the ledger is itself a read-modify-write
        # of the same running totals.
        locked.role = role_subscription.role
        advance_usage_ledger(locked, as_of)


def apply_refund(role_subscription: RoleSubscription, amount_refunded_cents: int) -> None:
    """Claw back a refunded (or lost-dispute) amount from the banked balance, in full.
    Policy: the refunded amount comes straight out of ``total_paid_cents`` (clamped at zero), so future periods simply stop being affordable.

    Args:
        role_subscription: The subscription to debit.
        amount_refunded_cents: The amount refunded or lost to a dispute, in cents."""
    if not role_subscription.role.pay_what_you_want or amount_refunded_cents <= 0:
        return
    with _locked(role_subscription) as locked:
        locked.total_paid_cents = max(0, locked.total_paid_cents - amount_refunded_cents)
        locked.save(update_fields=["total_paid_cents", "updated"])
