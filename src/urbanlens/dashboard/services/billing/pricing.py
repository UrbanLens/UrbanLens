"""Pure money/threshold helpers for paid subscriptions - no Stripe or network calls.

Kept separate from ``models.subscriptions.model.SubscriptionRole`` so the model itself
doesn't need a dependency on ``services.admin.cost_tracking``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

#: Stripe's own minimum charge amount for USD, in cents.
STRIPE_MINIMUM_CHARGE_CENTS = 50


def dollars_to_cents(dollars: Decimal | str) -> int:
    """Convert a dollar amount to integer cents, rounding to the nearest cent."""
    return int((Decimal(dollars) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def cents_to_dollars(cents: int) -> Decimal:
    """Convert integer cents to a dollar Decimal."""
    return Decimal(cents) / 100


def role_pwyw_threshold_cents(role: SubscriptionRole, as_of: datetime.datetime | None = None) -> int | None:
    """The pledge amount (cents) required for *role* to grant its features right now.

    Args:
        role: The role to evaluate.
        as_of: Point in time to evaluate the dynamic (cost-per-user) threshold against;
            defaults to now. Ignored for the static-minimum case.

    Returns:
        None when the role isn't pay-what-you-want (the concept doesn't apply - a fixed
        or admin-only role's ``RoleSubscription.threshold_met`` should just stay True).
        Otherwise the minimum pledge, in cents: the site's current cost-per-user when
        ``pwyw_dynamic_threshold`` is on, else the role's static ``pwyw_minimum_cents``
        (0 when unset - any pledge grants it).
    """
    if not role.pay_what_you_want:
        return None
    if role.pwyw_dynamic_threshold:
        from urbanlens.dashboard.services.admin.cost_tracking import cost_per_user

        cost = cost_per_user(as_of)
        return dollars_to_cents(cost) if cost is not None else 0
    return role.pwyw_minimum_cents or 0


def pledge_meets_threshold(role: SubscriptionRole, pledged_amount_cents: int, as_of: datetime.datetime | None = None) -> bool:
    """Whether a pledge currently clears *role*'s access threshold.

    Non-PWYW roles (``role_pwyw_threshold_cents`` returns None) always meet it once the
    subscription itself is active - there's no pledge-amount gate to fail.
    """
    threshold = role_pwyw_threshold_cents(role, as_of)
    return threshold is None or pledged_amount_cents >= threshold
