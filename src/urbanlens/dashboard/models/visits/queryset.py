"""QuerySet and manager for PinVisit."""
from __future__ import annotations

from urbanlens.dashboard.models import abstract


class VisitQuerySet(abstract.QuerySet):
    """QuerySet for PinVisit records."""

    def for_pin(self, pin_id: int) -> VisitQuerySet:
        """Filter to visits for a specific pin.

        Args:
            pin_id: Primary key of the pin.

        Returns:
            Filtered queryset.
        """
        return self.filter(pin_id=pin_id)

    def manual(self) -> VisitQuerySet:
        """Filter to manually-recorded visits.

        Returns:
            Filtered queryset.
        """
        return self.filter(source="manual")

    def from_takeout(self) -> VisitQuerySet:
        """Filter to visits imported from Google Takeout.

        Returns:
            Filtered queryset.
        """
        return self.filter(source="google_takeout")


class VisitManager(abstract.Manager.from_queryset(VisitQuerySet)):
    """Manager for PinVisit."""
