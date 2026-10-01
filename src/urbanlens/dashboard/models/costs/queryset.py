"""CostComponent and OperatingCost querysets and managers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.costs.model import CostComponent, OperatingCost  # noqa: F401 - mypy needs these; ruff does not


class CostComponentQuerySet(abstract.DashboardQuerySet["CostComponent"]):
    """Filters for admin-defined depreciating cost components."""


_CostComponentManagerBase = abstract.DashboardManager.from_queryset(CostComponentQuerySet)


class CostComponentManager(_CostComponentManagerBase["CostComponent"]):
    pass


class OperatingCostQuerySet(abstract.DashboardQuerySet["OperatingCost"]):
    """Filters for admin-defined recurring monthly operating costs."""


_OperatingCostManagerBase = abstract.DashboardManager.from_queryset(OperatingCostQuerySet)


class OperatingCostManager(_OperatingCostManagerBase["OperatingCost"]):
    pass
