"""CostComponent and OperatingCost querysets and managers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.costs.model import CostComponent, OperatingCost  # noqa: F401 - mypy needs these; ruff does not


class CostComponentQuerySet(abstract.DashboardQuerySet["CostComponent"]):
    """Filters for admin-defined depreciating cost components."""


class CostComponentManager(abstract.DashboardManager.from_queryset(CostComponentQuerySet)):
    pass


class OperatingCostQuerySet(abstract.DashboardQuerySet["OperatingCost"]):
    """Filters for admin-defined recurring monthly operating costs."""


class OperatingCostManager(abstract.DashboardManager.from_queryset(OperatingCostQuerySet)):
    pass
