"""Campus queryset and manager."""

from __future__ import annotations

import logging
from typing import Self

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class CampusQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Campus - spatial region data for a Location or Pin.

    Campus is distinct from Location (canonical place data) and Pin (user visit
    records).  Filters here operate on region/boundary data.
    """

    def defaults(self) -> Self:
        """Location-level default campuses (profile=None, pin=None)."""
        return self.filter(profile__isnull=True, pin__isnull=True)

    def for_profile(self, profile) -> Self:
        """Pin-scoped campuses belonging to a given profile."""
        return self.filter(profile=profile, pin__isnull=False)

    def for_location(self, location) -> Self:
        """All campuses (default and pin-scoped) referencing a given location."""
        return self.filter(location=location)

    def for_pin(self, pin) -> Self:
        """Pin-scoped campus for a specific pin."""
        return self.filter(pin=pin)

    def with_location(self) -> Self:
        """Prefetch location so effective_polygon doesn't trigger extra queries."""
        return self.select_related("location")


class CampusManager(abstract.DashboardManager.from_queryset(CampusQuerySet)):
    """Manager for Campus.

    Use effective_for(location) for location wiki lookups and
    effective_for_pin(pin) for pin detail lookups.
    """

    def effective_for(self, location, profile=None):
        """Return the location-default Campus for a given location.

        Only location-default campuses (pin=None, profile=None) are considered.
        Pin-scoped boundaries are resolved separately via effective_for_pin().

        Args:
            location: Location instance or pk.
            profile: Ignored; kept for backwards compatibility.

        Returns:
            Campus | None
        """
        return self.filter(location=location, profile__isnull=True, pin__isnull=True).select_related("location").first()

    def effective_for_pin(self, pin):
        """Return the Campus to display for a given pin.

        Resolution order:
        1. Pin-scoped campus for this pin (if one exists).
        2. Location-default campus (profile=None, pin=None) for pin.location.
        3. None - caller should fall back to a generated circle.

        Args:
            pin: Pin instance (must have location_id accessible).

        Returns:
            Campus | None
        """
        if pin_campus := self.filter(pin=pin).select_related("location").first():
            return pin_campus
        if pin.location_id:
            return self.filter(wiki__location_id=pin.location_id, profile__isnull=True, pin__isnull=True).select_related("location").first()
        return None
