"""CategoryQuerySet and CategoryManager are now backed by TagQuerySet."""

from __future__ import annotations

from urbanlens.dashboard.models.tags.queryset import TagManager, TagQuerySet

CategoryQuerySet = TagQuerySet
CategoryManager = TagManager

__all__ = ["CategoryManager", "CategoryQuerySet"]
