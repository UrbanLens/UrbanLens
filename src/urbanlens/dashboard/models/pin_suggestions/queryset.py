"""QuerySet and manager for PinSuggestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class PinSuggestionQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PinSuggestion records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to suggestions belonging to a given profile."""
        return self.filter(profile=profile)

    def pending(self) -> Self:
        """Filter to suggestions still awaiting a response."""
        from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestionStatus

        return self.filter(status=PinSuggestionStatus.PENDING)

    def matched(self) -> Self:
        """Filter to suggestions for an existing pin."""
        return self.filter(pin__isnull=False)

    def new_pin(self) -> Self:
        """Filter to suggestions proposing a brand-new pin."""
        return self.filter(pin__isnull=True)


class PinSuggestionManager(abstract.DashboardManager.from_queryset(PinSuggestionQuerySet)):
    """Manager for PinSuggestion."""
