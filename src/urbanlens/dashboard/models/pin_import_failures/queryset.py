"""QuerySet and manager for PinImportFailure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class PinImportFailureQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PinImportFailure records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to failures belonging to a given profile.

        Args:
            profile: Owner profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(profile=profile)

    def pending(self) -> Self:
        """Filter to failures still awaiting a response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailureStatus

        return self.filter(status=PinImportFailureStatus.PENDING)


class PinImportFailureManager(abstract.DashboardManager.from_queryset(PinImportFailureQuerySet)):
    """Manager for PinImportFailure."""
