"""LocationEdit queryset and manager."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class LocationEditQuerySet(abstract.QuerySet):
    """QuerySet for community Location wiki edit history."""

    def for_location(self, location) -> Self:
        """Filter edits for a given location."""
        return self.filter(location=location)

    def active(self) -> Self:
        """Return edits that have not been reverted."""
        return self.filter(reverted=False)


class LocationEditManager(abstract.Manager.from_queryset(LocationEditQuerySet)):
    """Manager for LocationEdit audit records."""
