"""QuerySet and manager for VisitSuggestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class VisitSuggestionQuerySet(abstract.QuerySet):
    """QuerySet for VisitSuggestion records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to suggestions sent to a given profile.

        Args:
            profile: Recipient profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(suggested_to=profile)

    def pending(self) -> Self:
        """Filter to suggestions still awaiting a response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestionStatus

        return self.filter(status=VisitSuggestionStatus.PENDING)


class VisitSuggestionManager(abstract.Manager.from_queryset(VisitSuggestionQuerySet)):
    """Manager for VisitSuggestion."""
