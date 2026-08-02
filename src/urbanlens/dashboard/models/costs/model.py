"""Admin-defined cost tracking: depreciating components and recurring operating costs."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.validators import MinValueValidator
from django.db.models import DateTimeField, DecimalField, Index, IntegerField, TextField
from django.db.models.fields import CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.costs.queryset import CostComponentManager, OperatingCostManager

if TYPE_CHECKING:
    import datetime


class CostComponent(abstract.DashboardModel):
    """A depreciating hardware/infrastructure asset an admin has defined, e.g. "Hard Drives".

    Its replacement cost is amortized evenly over ``deprecation_years`` to produce a
    monthly figure (see ``monthly_amortized_cost``) that feeds into the site's overall
    effective monthly running cost. ``retired_at`` records when a component stopped
    counting toward that figure - it is kept (not deleted) so all-time expense totals
    stay accurate.

    Attributes:
        name: Display name, e.g. "Hard Drives".
        replacement_cost: One-time cost to replace this component, in USD.
        deprecation_years: How many years the replacement cost is spread over.
        notes: Optional free-text context for admins.
        order: Manual display ordering; lower sorts first.
        retired_at: When this component stopped counting; null means still active.
    """

    name = CharField(max_length=255)
    replacement_cost = DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal(0))],
        help_text="One-time replacement cost in USD, e.g. 1000.00.",
    )
    deprecation_years = DecimalField(
        max_digits=5,
        decimal_places=1,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text="Years over which the replacement cost is amortized, e.g. 10.0.",
    )
    notes = TextField(blank=True)
    order = IntegerField(default=0)
    retired_at = DateTimeField(null=True, blank=True, help_text="When this component stopped counting toward costs. Blank means still active.")

    objects = CostComponentManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_cost_components"
        verbose_name = "Cost Component"
        verbose_name_plural = "Cost Components"
        ordering = ["order", "name"]
        indexes = [
            Index(fields=["retired_at"], name="idxdb_costcmp_retired"),
        ]

    def __str__(self) -> str:
        return f"{self.name} (${self.replacement_cost} / {self.deprecation_years}yr)"

    @property
    def is_active(self) -> bool:
        """Whether this component is still depreciating (not retired)."""
        return self.retired_at is None

    @property
    def monthly_amortized_cost(self) -> Decimal:
        """This component's replacement cost spread evenly across its deprecation life."""
        return self.replacement_cost / (self.deprecation_years * 12)

    def elapsed_months(self, as_of: datetime.datetime) -> Decimal:
        """Months this component has counted toward costs, from creation to retirement (or *as_of*).

        Args:
            as_of: The point in time to measure elapsed months up to.

        Returns:
            Elapsed months as a Decimal, using a 30-day month approximation
            (consistent with the rest of the admin stats reporting).
        """
        end = min(self.retired_at, as_of) if self.retired_at else as_of
        elapsed_days = max(Decimal((end - self.created).days), Decimal(0))
        return elapsed_days / Decimal(30)


class OperatingCost(abstract.DashboardModel):
    """A recurring monthly operating cost an admin has defined, e.g. "Electricity".

    ``retired_at`` records when a cost stopped being charged - it is kept (not
    deleted) so all-time expense totals stay accurate.

    Attributes:
        name: Display name, e.g. "Electricity".
        monthly_cost: Recurring monthly cost in USD.
        notes: Optional free-text context for admins.
        order: Manual display ordering; lower sorts first.
        retired_at: When this cost stopped being charged; null means still active.
    """

    name = CharField(max_length=255)
    monthly_cost = DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal(0))],
        help_text="Recurring monthly cost in USD, e.g. 100.00.",
    )
    notes = TextField(blank=True)
    order = IntegerField(default=0)
    retired_at = DateTimeField(null=True, blank=True, help_text="When this cost stopped being charged. Blank means still active.")

    objects = OperatingCostManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_operating_costs"
        verbose_name = "Operating Cost"
        verbose_name_plural = "Operating Costs"
        ordering = ["order", "name"]
        indexes = [
            Index(fields=["retired_at"], name="idxdb_opcost_retired"),
        ]

    def __str__(self) -> str:
        return f"{self.name} (${self.monthly_cost}/mo)"

    @property
    def is_active(self) -> bool:
        """Whether this cost is still being charged (not retired)."""
        return self.retired_at is None

    def elapsed_months(self, as_of: datetime.datetime) -> Decimal:
        """Months this cost has been charged, from creation to retirement (or *as_of*).

        Args:
            as_of: The point in time to measure elapsed months up to.

        Returns:
            Elapsed months as a Decimal, using a 30-day month approximation
            (consistent with the rest of the admin stats reporting).
        """
        end = min(self.retired_at, as_of) if self.retired_at else as_of
        elapsed_days = max(Decimal((end - self.created).days), Decimal(0))
        return elapsed_days / Decimal(30)
