"""QuerySet and manager for PinSuggestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.profile.model import Profile


class PinSuggestionQuerySet(abstract.DashboardQuerySet["PinSuggestion"]):
    """QuerySet for PinSuggestion records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to suggestions belonging to a given profile.

        Args:
            profile: Owner profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(profile=profile)

    def pending(self) -> Self:
        """Filter to suggestions still awaiting a response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestionStatus

        return self.filter(status=PinSuggestionStatus.PENDING)


_PinSuggestionManagerBase = abstract.DashboardManager.from_queryset(PinSuggestionQuerySet)


class PinSuggestionManager(_PinSuggestionManagerBase):
    """Manager for PinSuggestion."""
