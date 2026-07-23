"""QuerySets and Managers for PinOwner/WikiOwner and PinPropertySale/WikiPropertySale."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class PinOwnerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PinOwner."""

    def for_pin(self, pin: Pin) -> Self:
        """Return the owners private to a specific pin.

        Args:
            pin: The pin to filter by.

        Returns:
            Owners attached to that pin.
        """
        return self.filter(pin=pin)


class PinOwnerManager(abstract.DashboardManager.from_queryset(PinOwnerQuerySet)):
    """Manager for PinOwner."""


class WikiOwnerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for WikiOwner."""

    def for_location(self, location: Location) -> Self:
        """Return the owners shared for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            Owners linked to that location.
        """
        return self.filter(locations=location)


class WikiOwnerManager(abstract.DashboardManager.from_queryset(WikiOwnerQuerySet)):
    """Manager for WikiOwner."""


class PinPropertySaleQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PinPropertySale."""

    def for_pin(self, pin: Pin) -> Self:
        """Return the sale records private to a specific pin.

        Args:
            pin: The pin to filter by.

        Returns:
            Sales for that pin, newest first (model default ordering).
        """
        return self.filter(pin=pin)


class PinPropertySaleManager(abstract.DashboardManager.from_queryset(PinPropertySaleQuerySet)):
    """Manager for PinPropertySale."""


class WikiPropertySaleQuerySet(abstract.DashboardQuerySet):
    """QuerySet for WikiPropertySale."""

    def for_location(self, location: Location) -> Self:
        """Return the sale records shared for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            Sales for that location, newest first (model default ordering).
        """
        return self.filter(location=location)


class WikiPropertySaleManager(abstract.DashboardManager.from_queryset(WikiPropertySaleQuerySet)):
    """Manager for WikiPropertySale."""
