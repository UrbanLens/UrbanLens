"""QuerySets and Managers for Owner and PropertySale."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.property_owner.meta import OwnerVisibility

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class OwnerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Owner."""

    def for_location(self, location: Location) -> Self:
        """Return the shared owners associated with a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            SHARED-visibility owners linked to that location.
        """
        return self.filter(visibility=OwnerVisibility.SHARED, locations=location)

    def visible_on(self, pin: Pin) -> Self:
        """Return every owner visible while viewing a specific pin.

        Args:
            pin: The pin being viewed.

        Returns:
            The location's SHARED owners, plus any PRIVATE owners attached to
            this exact pin (private owners on a different pin, even at the
            same location, are excluded).
        """
        return self.filter(Q(visibility=OwnerVisibility.SHARED, locations=pin.location_id) | Q(visibility=OwnerVisibility.PRIVATE, pins=pin)).distinct()


class OwnerManager(abstract.DashboardManager.from_queryset(OwnerQuerySet)):
    """Manager for Owner."""


class PropertySaleQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PropertySale."""

    def for_location(self, location: Location) -> Self:
        """Return the shared sale records for a specific location.

        Args:
            location: The Location to filter by.

        Returns:
            SHARED-visibility sales for that location, newest first (model default ordering).
        """
        return self.filter(visibility=OwnerVisibility.SHARED, location=location)

    def visible_on(self, pin: Pin) -> Self:
        """Return every sale record visible while viewing a specific pin.

        Args:
            pin: The pin being viewed.

        Returns:
            The location's SHARED sales, plus any PRIVATE sales attached to
            this exact pin, newest first.
        """
        return self.filter(Q(visibility=OwnerVisibility.SHARED, location=pin.location_id) | Q(visibility=OwnerVisibility.PRIVATE, pin=pin))


class PropertySaleManager(abstract.DashboardManager.from_queryset(PropertySaleQuerySet)):
    """Manager for PropertySale."""
