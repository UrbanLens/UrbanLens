"""QuerySet and manager for VisitSuggestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from decimal import Decimal

    from urbanlens.dashboard.models.location.model import Location
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

    def for_place(self, *, location: Location | None, latitude: Decimal | float | None, longitude: Decimal | float | None) -> Self:
        """Filter to suggestions for a specific place.

        Matches on the shared Location when one is given, otherwise falls back
        to an exact latitude/longitude match (mirrors how ``find_pin_at``
        resolves a profile's own pin for a place with no Location).

        Args:
            location: Shared Location identifying the place, if one exists.
            latitude: Latitude to match on when there is no location.
            longitude: Longitude to match on when there is no location.

        Returns:
            Filtered queryset.
        """
        if location is not None:
            return self.filter(location=location)
        return self.filter(latitude=latitude, longitude=longitude)

    def pending(self) -> Self:
        """Filter to suggestions still awaiting a response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestionStatus

        return self.filter(status=VisitSuggestionStatus.PENDING)


class VisitSuggestionManager(abstract.Manager.from_queryset(VisitSuggestionQuerySet)):
    """Manager for VisitSuggestion."""
