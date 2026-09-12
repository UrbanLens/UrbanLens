"""QuerySet and manager for PinVisit."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class VisitQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for PinVisit records."""

    def for_pin(self, pin_id: int) -> Self:
        """Filter to visits for a specific pin."""
        return self.filter(pin_id=pin_id)

    def manual(self) -> Self:
        """Filter to manually-recorded visits."""
        from urbanlens.dashboard.models.visits.model import VisitSource

        return self.filter(source=VisitSource.MANUAL)

    def from_takeout(self) -> Self:
        """Filter to visits from the user's location history."""
        from urbanlens.dashboard.models.visits.model import VisitSource

        return self.filter(source=VisitSource.HISTORY)


class VisitManager(abstract.FrontendDashboardManager.from_queryset(VisitQuerySet)):
    """Manager for PinVisit."""
