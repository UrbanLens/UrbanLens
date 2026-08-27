"""CostComponent and OperatingCost querysets and managers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.costs.model import CostComponent, OperatingCost


class CostComponentQuerySet(abstract.DashboardQuerySet["CostComponent"]):
    """Filters for admin-defined depreciating cost components."""

    def active(self) -> Self:
        """Return only components still depreciating (not retired)."""
        return self.filter(retired_at__isnull=True)


class CostComponentManager(abstract.DashboardManager.from_queryset(CostComponentQuerySet)):
    pass


class OperatingCostQuerySet(abstract.DashboardQuerySet["OperatingCost"]):
    """Filters for admin-defined recurring monthly operating costs."""

    def active(self) -> Self:
        """Return only operating costs still being charged (not retired)."""
        return self.filter(retired_at__isnull=True)


class OperatingCostManager(abstract.DashboardManager.from_queryset(OperatingCostQuerySet)):
    pass
