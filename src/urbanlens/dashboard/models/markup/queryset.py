"""Querysets and managers for MarkupMap and PinMarkup."""

from __future__ import annotations

import logging
from typing import Self

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class PinMarkupQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for PinMarkup map annotations (lines, arrows, text labels)."""

    def for_pin(self, pin) -> Self:
        """All markup items belonging to a specific parent pin."""
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki) -> Self:
        """All shared/community markup items belonging to a specific Wiki."""
        return self.filter(parent_wiki=wiki)

    def for_map(self, markup_map) -> Self:
        """All markup items belonging to a specific MarkupMap."""
        return self.filter(parent_map=markup_map)

    def for_profile(self, profile) -> Self:
        """All markup items belonging to a specific profile."""
        return self.filter(profile=profile)


class PinMarkupManager(abstract.FrontendDashboardManager.from_queryset(PinMarkupQuerySet)):
    """Manager for PinMarkup."""


class MarkupMapQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for standalone MarkupMap containers."""

    def for_profile(self, profile) -> Self:
        """All markup maps owned by a specific profile."""
        return self.filter(profile=profile)

    def unattached(self) -> Self:
        """Maps not linked from any host model (drafts / leftovers).

        Returns:
            Maps with no safety check-in, comment, trip comment, or visit
            pointing at them.
        """
        return self.filter(
            safety_checkins__isnull=True,
            comments__isnull=True,
            trip_comments__isnull=True,
            visits__isnull=True,
        )


class MarkupMapManager(abstract.FrontendDashboardManager.from_queryset(MarkupMapQuerySet)):
    """Manager for MarkupMap."""
