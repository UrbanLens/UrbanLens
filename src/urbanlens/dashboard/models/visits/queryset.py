"""QuerySet and manager for PinVisit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class VisitQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for PinVisit records."""

    def for_pin(self, pin_id: int) -> Self:
        """Filter to visits for a specific pin.

        Args:
            pin_id: Primary key of the pin.

        Returns:
            Filtered queryset.
        """
        return self.filter(pin_id=pin_id)

    def owned_by(self, profile: Profile) -> Self:
        """Visits logged against *profile*'s own pins.

        The bound is the pin ids rather than ``pin__profile=profile``, because the latter is a
        join and Postgres is free to start it from the visit table: at capacity scale it does, a
        sequential scan of all 16,208 visits hash-joined back to the viewer's pins, so one
        account's visit search costs the site's visit count. See :mod:`urbanlens.core.semijoin`.

        Args:
            profile: The owning profile.

        Returns:
            Their visits.
        """
        from urbanlens.dashboard.models.pin.model import Pin

        return self.bounded_by("pin", Pin.objects.filter(profile=profile))

    def manual(self) -> Self:
        """Filter to manually-recorded visits.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.visits.model import VisitSource

        return self.filter(source=VisitSource.MANUAL)

    def from_takeout(self) -> Self:
        """Filter to visits from the user's location history.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.visits.model import VisitSource

        return self.filter(source=VisitSource.HISTORY)


class VisitManager(abstract.FrontendDashboardManager.from_queryset(VisitQuerySet)):
    """Manager for PinVisit."""
