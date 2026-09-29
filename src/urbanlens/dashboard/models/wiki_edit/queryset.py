"""WikiEdit queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit  # noqa: F401 - mypy needs these; ruff does not


class WikiEditQuerySet(abstract.DashboardQuerySet["WikiEdit"]):
    """QuerySet for community Wiki edit history."""

    def for_wiki(self, wiki) -> Self:
        """Filter edits for a given wiki."""
        return self.filter(wiki=wiki)

    def active(self) -> Self:
        """Return edits that have not been reverted."""
        return self.filter(reverted=False)


_WikiEditManagerBase = abstract.DashboardManager.from_queryset(WikiEditQuerySet)


class WikiEditManager(_WikiEditManagerBase):
    """Manager for WikiEdit audit records."""
