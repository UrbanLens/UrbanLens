"""QuerySet and Manager for Comment."""
from __future__ import annotations

from django.db.models import Manager, QuerySet


class CommentQuerySet(QuerySet):

    def top_level(self) -> CommentQuerySet:
        """Return only top-level comments (not replies)."""
        return self.filter(parent__isnull=True)

    def for_pin(self, pin) -> CommentQuerySet:
        return self.filter(pin=pin, parent__isnull=True)

    def for_location(self, location) -> CommentQuerySet:
        return self.filter(location=location, parent__isnull=True)


class CommentManager(Manager.from_queryset(CommentQuerySet)):
    pass
