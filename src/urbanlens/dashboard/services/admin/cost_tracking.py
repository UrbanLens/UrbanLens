"""Cost tracking calculations: admin-defined hardware/operating costs plus tracked API spend.

Every function accepts an optional ``as_of`` so callers (and tests) can evaluate the
numbers at a specific point in time instead of always "now" - useful for reconstructing
historical series without needing to mock the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from urbanlens.dashboard.models.costs import CostComponent, OperatingCost

if TYPE_CHECKING:
    import datetime

_ZERO = Decimal(0)
_API_COST_WINDOW_DAYS = 30
_ACTIVE_USER_WINDOW_DAYS = 30
_ACTIVE_USER_MIN_ROOT_PINS = 2


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The effective monthly cost to run the site, split by source."""

    hardware: Decimal
    operating: Decimal
    api: Decimal

    @property
    def total(self) -> Decimal:
        return self.hardware + self.operating + self.api


def _components_active_as_of(as_of: datetime.datetime):
    """Return CostComponents that existed and weren't yet retired at *as_of*."""
    return CostComponent.objects.filter(created__lte=as_of).filter(Q(retired_at__isnull=True) | Q(retired_at__gt=as_of))


def _operating_costs_active_as_of(as_of: datetime.datetime):
    """Return OperatingCosts that existed and weren't yet retired at *as_of*."""
    return OperatingCost.objects.filter(created__lte=as_of).filter(Q(retired_at__isnull=True) | Q(retired_at__gt=as_of))


def _api_cost_in_window(start: datetime.datetime, end: datetime.datetime) -> Decimal:
    """Return the sum of ``ApiCallLog.cost_estimate`` recorded in ``[start, end]``."""
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    total = ApiCallLog.objects.filter(created__gte=start, created__lte=end).aggregate(total=Sum("cost_estimate"))["total"]
    return total or _ZERO


def effective_monthly_cost(as_of: datetime.datetime | None = None) -> CostBreakdown:
    """Return the current effective monthly cost to run the site, broken out by source.

    Args:
        as_of: Point in time to evaluate "currently active" against; defaults to now.

    Returns:
        A CostBreakdown with hardware (amortized components), operating (recurring
        operating costs), and api (trailing-30-day actual API spend) figures.
    """
    as_of = as_of or timezone.now()

    hardware = sum(
        (component.monthly_amortized_cost for component in _components_active_as_of(as_of)),
        start=_ZERO,
    )
    operating = sum(
        (cost.monthly_cost for cost in _operating_costs_active_as_of(as_of)),
        start=_ZERO,
    )
    api = _api_cost_in_window(as_of - timedelta(days=_API_COST_WINDOW_DAYS), as_of)

    return CostBreakdown(hardware=hardware, operating=operating, api=api)


def total_hardware_cost() -> Decimal:
    """Return the total (non-amortized) replacement cost of every hardware component on record.

    Unlike ``effective_monthly_cost().hardware``, this isn't divided by each component's
    deprecation period - it's the actual amount spent, including retired components (the
    money was already spent regardless of retirement, same convention as
    ``total_recorded_expenses`` below).

    Returns:
        The all-time hardware total, in USD.
    """
    return CostComponent.objects.aggregate(total=Sum("replacement_cost"))["total"] or _ZERO


def total_recorded_expenses(as_of: datetime.datetime | None = None) -> Decimal:
    """Return total money spent to date across every cost source.

    Sums one-time component replacement costs (counted once, regardless of
    retirement - the money was already spent), recurring operating costs accumulated
    over their active lifespan, and actual recorded API spend.

    Args:
        as_of: Point in time to total expenses up to; defaults to now.

    Returns:
        The all-time total, in USD.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    as_of = as_of or timezone.now()

    hardware_total = total_hardware_cost()
    operating_total = sum(
        (cost.monthly_cost * cost.elapsed_months(as_of) for cost in OperatingCost.objects.all()),
        start=_ZERO,
    )
    api_total = ApiCallLog.objects.filter(created__lte=as_of).aggregate(total=Sum("cost_estimate"))["total"] or _ZERO

    return hardware_total + operating_total + api_total


def average_monthly_expense(as_of: datetime.datetime | None = None) -> Decimal | None:
    """Return the historical average monthly expense.

    ``total_recorded_expenses`` divided by the number of months since the earliest
    cost record (component, operating cost, or API call).

    Args:
        as_of: Point in time to evaluate against; defaults to now.

    Returns:
        The average, or None when there is no cost data yet.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    as_of = as_of or timezone.now()

    earliest_candidates = [
        CostComponent.objects.order_by("created").values_list("created", flat=True).first(),
        OperatingCost.objects.order_by("created").values_list("created", flat=True).first(),
        ApiCallLog.objects.order_by("created").values_list("created", flat=True).first(),
    ]
    earliest = min((value for value in earliest_candidates if value is not None), default=None)
    if earliest is None:
        return None

    elapsed_days = max((as_of - earliest).days, 0)
    months = max(Decimal(elapsed_days) / Decimal(30), Decimal(1))
    return total_recorded_expenses(as_of) / months


def active_user_count(as_of: datetime.datetime | None = None) -> int:
    """Return the number of "active" users, for cost-per-user calculations.

    An active user is one who logged in within the trailing 30 days *and* has placed
    at least 2 (root) pins - a one-time signup who created an account and never
    returned would otherwise dilute the per-user figure and understate the real cost
    of an engaged user.

    Args:
        as_of: Point in time to evaluate the 30-day login window against; defaults to now.

    Returns:
        The number of active users.
    """
    as_of = as_of or timezone.now()
    window_start = as_of - timedelta(days=_ACTIVE_USER_WINDOW_DAYS)
    return (
        User.objects.filter(is_active=True, last_login__gte=window_start)
        .annotate(root_pin_count=Count("profile__pins", filter=Q(profile__pins__parent_pin__isnull=True), distinct=True))
        .filter(root_pin_count__gte=_ACTIVE_USER_MIN_ROOT_PINS)
        .count()
    )


def cost_per_user(as_of: datetime.datetime | None = None) -> Decimal | None:
    """Return the current effective monthly cost divided by active users.

    Args:
        as_of: Point in time to evaluate against; defaults to now.

    Returns:
        The per-user monthly cost, or None when there are no active users.
    """
    users = active_user_count(as_of)
    if not users:
        return None
    return effective_monthly_cost(as_of).total / users


def _advance_month(cursor: datetime.datetime) -> datetime.datetime:
    """Return the first moment of the calendar month after *cursor*."""
    if cursor.month == 12:
        return cursor.replace(year=cursor.year + 1, month=1)
    return cursor.replace(month=cursor.month + 1)


def monthly_cost_series(months: int = 12, as_of: datetime.datetime | None = None) -> dict[str, list]:
    """Reconstruct the effective monthly cost for each of the last *months* calendar months.

    For each month, sums the amortized cost of every component and the monthly cost of
    every operating cost that existed and wasn't yet retired at any point during that
    month (from their ``created``/``retired_at`` timestamps), plus the actual API spend
    recorded that month. There is no separate expense ledger - this is a reconstruction
    from the admin-defined items' own timestamps, consistent with how the rest of the
    admin stats pages build monthly series.

    Args:
        months: How many trailing calendar months to include.
        as_of: The "current" month to count backward from; defaults to now.

    Returns:
        ``{"labels": [...], "hardware": [...], "operating": [...], "api": [...]}``,
        one entry per month, oldest first.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    as_of = as_of or timezone.now()
    start = (as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=(months - 1) * 30)).replace(day=1)

    components = list(CostComponent.objects.all())
    operating_costs = list(OperatingCost.objects.all())

    api_rows = {
        row["_month"]: row["total"]
        for row in (ApiCallLog.objects.filter(created__gte=start).annotate(_month=TruncMonth("created")).values("_month").annotate(total=Sum("cost_estimate")))
    }

    labels: list[str] = []
    hardware: list[float] = []
    operating: list[float] = []
    api: list[float] = []

    cursor = start
    for _ in range(months):
        next_cursor = _advance_month(cursor)
        labels.append(cursor.strftime("%b %Y"))
        hardware.append(
            float(
                sum(
                    (c.monthly_amortized_cost for c in components if c.created < next_cursor and (c.retired_at is None or c.retired_at > cursor)),
                    start=_ZERO,
                )
            )
        )
        operating.append(
            float(
                sum(
                    (c.monthly_cost for c in operating_costs if c.created < next_cursor and (c.retired_at is None or c.retired_at > cursor)),
                    start=_ZERO,
                )
            )
        )
        api.append(float(api_rows.get(cursor.replace(tzinfo=None), api_rows.get(cursor, 0)) or 0))
        cursor = next_cursor

    return {"labels": labels, "hardware": hardware, "operating": operating, "api": api}


def api_spend_summary_30d() -> dict[str, list | int | Decimal | None]:
    """Return the trailing-30-day external API spend, split by pricing status.

    Only services with a confirmed per-call price (``ServiceDefaults.cost_per_call``)
    contribute to the total - the rest are either free, priced in a way that doesn't
    reduce to one number per call, or not priced here yet, and are only reflected in
    ``unpriced_service_count``. Shared by the site-admin cost tracking page and the
    public running-costs page.

    Returns:
        ``{"priced_services": [{"display_name": str, "cost_30d": Decimal}, ...],
        "unpriced_service_count": int, "total_cost_30d": Decimal | None}``, with
        ``priced_services`` sorted by cost descending.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog
    from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

    service_defaults = all_service_defaults()
    summaries = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

    priced_services = []
    unpriced_service_count = 0
    total_cost_30d = None
    for service, defaults in service_defaults.items():
        if defaults.cost_per_call is None:
            unpriced_service_count += 1
            continue
        row = summaries.get(service, {})
        cost_30d = row.get("total_cost")
        if cost_30d is None:
            continue
        total_cost_30d = cost_30d if total_cost_30d is None else total_cost_30d + cost_30d
        priced_services.append({"display_name": defaults.display_name, "cost_30d": cost_30d})

    priced_services.sort(key=lambda entry: entry["cost_30d"], reverse=True)
    return {
        "priced_services": priced_services,
        "unpriced_service_count": unpriced_service_count,
        "total_cost_30d": total_cost_30d,
    }
