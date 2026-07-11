"""QuerySet and Manager for Comment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


class CommentQuerySet(abstract.FrontendDashboardQuerySet):
    def top_level(self) -> Self:
        """Return only top-level comments (not replies)."""
        return self.filter(parent__isnull=True)

    def for_pin(self, pin: Pin) -> Self:
        return self.filter(pin=pin, parent__isnull=True)

    def for_wiki(self, wiki: Wiki) -> Self:
        return self.filter(wiki=wiki, parent__isnull=True)


class CommentManager(abstract.FrontendDashboardManager.from_queryset(CommentQuerySet)):
    pass
