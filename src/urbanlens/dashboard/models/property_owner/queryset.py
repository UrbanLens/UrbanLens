"""QuerySets and Managers for PinOwner/WikiOwner and PinPropertySale/WikiPropertySale."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale, WikiOwner, WikiPropertySale  # noqa: F401 - mypy needs these; ruff does not


class PinOwnerQuerySet(abstract.DashboardQuerySet["PinOwner"]):
    """QuerySet for PinOwner."""

    def for_pin(self, pin: Pin) -> Self:
        """Return the owners private to a specific pin.

        Args:
            pin: The pin to filter by.

        Returns:
            Owners attached to that pin.
        """
        return self.filter(pin=pin)


_PinOwnerManagerBase = abstract.DashboardManager.from_queryset(PinOwnerQuerySet)


class PinOwnerManager(_PinOwnerManagerBase):
    """Manager for PinOwner."""


class WikiOwnerQuerySet(abstract.DashboardQuerySet["WikiOwner"]):
    """QuerySet for WikiOwner."""

    def for_location(self, location: Location) -> Self:
        """Return the owners shared for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            Owners linked to that location.
        """
        return self.filter(locations=location)


_WikiOwnerManagerBase = abstract.DashboardManager.from_queryset(WikiOwnerQuerySet)


class WikiOwnerManager(_WikiOwnerManagerBase):
    """Manager for WikiOwner."""


class PinPropertySaleQuerySet(abstract.DashboardQuerySet["PinPropertySale"]):
    """QuerySet for PinPropertySale."""

    def for_pin(self, pin: Pin) -> Self:
        """Return the sale records private to a specific pin.

        Args:
            pin: The pin to filter by.

        Returns:
            Sales for that pin, newest first (model default ordering).
        """
        return self.filter(pin=pin)


_PinPropertySaleManagerBase = abstract.DashboardManager.from_queryset(PinPropertySaleQuerySet)


class PinPropertySaleManager(_PinPropertySaleManagerBase):
    """Manager for PinPropertySale."""


class WikiPropertySaleQuerySet(abstract.DashboardQuerySet["WikiPropertySale"]):
    """QuerySet for WikiPropertySale."""

    def for_location(self, location: Location) -> Self:
        """Return the sale records shared for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            Sales for that location, newest first (model default ordering).
        """
        return self.filter(location=location)


_WikiPropertySaleManagerBase = abstract.DashboardManager.from_queryset(WikiPropertySaleQuerySet)


class WikiPropertySaleManager(_WikiPropertySaleManagerBase):
    """Manager for WikiPropertySale."""
