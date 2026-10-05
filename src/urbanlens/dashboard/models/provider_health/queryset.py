"""QuerySet and Manager for ProviderHealth."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models.abstract.queryset import DashboardManager, DashboardQuerySet
from urbanlens.dashboard.models.provider_health.meta import ProviderState

if TYPE_CHECKING:
    from urbanlens.dashboard.models.provider_health.model import ProviderHealth  # noqa: F401 - mypy needs these; ruff does not


class ProviderHealthQuerySet(DashboardQuerySet["ProviderHealth"]):
    """QuerySet for ProviderHealth."""

    def unhealthy(self) -> Self:
        """Providers not currently ``healthy``: the ones anything is paused or alerted for."""
        return self.exclude(state=ProviderState.HEALTHY)


_ProviderHealthManagerBase = DashboardManager.from_queryset(ProviderHealthQuerySet)


class ProviderHealthManager(_ProviderHealthManagerBase):
    """Manager for ProviderHealth."""
