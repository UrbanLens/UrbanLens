"""WikiEdit queryset and manager."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class WikiEditQuerySet(abstract.DashboardQuerySet):
    """QuerySet for community Wiki edit history."""

    def for_wiki(self, wiki) -> Self:
        """Filter edits for a given wiki."""
        return self.filter(wiki=wiki)

    def active(self) -> Self:
        """Return edits that have not been reverted."""
        return self.filter(reverted=False)


class WikiEditManager(abstract.DashboardManager.from_queryset(WikiEditQuerySet)):
    """Manager for WikiEdit audit records."""
