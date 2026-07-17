"""QuerySet and Manager for LinkExtraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class LinkExtractionQuerySet(abstract.DashboardQuerySet):
    """Query helpers for :class:`~urbanlens.dashboard.models.link_extraction.model.LinkExtraction`."""

    def for_profile(self, profile: Profile) -> Self:
        """The given profile's extraction runs, newest first, ready for the review page.

        Args:
            profile: The requesting user.

        Returns:
            Filtered queryset ordered newest-first with the pin preselected.
        """
        return self.filter(profile=profile).select_related("pin", "pin__location").order_by("-created")

    def started_today(self, profile: Profile) -> Self:
        """Runs the profile started since local midnight - the daily-limit window.

        Every run counts against the limit regardless of how it ended (a failed
        AI call still consumed a fetch and possibly tokens), so this deliberately
        does not filter by status.

        Args:
            profile: The requesting user.

        Returns:
            Filtered queryset.
        """
        midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.filter(profile=profile, created__gte=midnight)


class LinkExtractionManager(abstract.DashboardManager.from_queryset(LinkExtractionQuerySet)):
    """Manager for LinkExtraction."""
