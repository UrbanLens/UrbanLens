"""QuerySet and Manager for Comment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Manager, QuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class CommentQuerySet(QuerySet):
    def top_level(self) -> Self:
        """Return only top-level comments (not replies)."""
        return self.filter(parent__isnull=True)

    def for_pin(self, pin: Pin) -> Self:
        return self.filter(pin=pin, parent__isnull=True)

    def for_location(self, location: Location) -> Self:
        return self.filter(location=location, parent__isnull=True)


class CommentManager(Manager.from_queryset(CommentQuerySet)):
    pass
