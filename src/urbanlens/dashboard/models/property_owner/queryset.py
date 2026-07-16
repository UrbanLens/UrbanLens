"""QuerySets and Managers for Owner and PropertySale."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


class OwnerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Owner."""

    def for_location(self, location: Location) -> Self:
        """Return owners associated with a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            Owners linked to that location.
        """
        return self.filter(locations=location)


class OwnerManager(abstract.DashboardManager.from_queryset(OwnerQuerySet)):
    """Manager for Owner."""


class PropertySaleQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PropertySale."""

    def for_location(self, location: Location) -> Self:
        """Return sale records for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            PropertySale rows for that location, newest first (model default ordering).
        """
        return self.filter(location=location)


class PropertySaleManager(abstract.DashboardManager.from_queryset(PropertySaleQuerySet)):
    """Manager for PropertySale."""
