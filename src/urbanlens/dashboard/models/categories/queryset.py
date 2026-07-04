"""CategoryQuerySet and CategoryManager are now backed by BadgeQuerySet."""

from __future__ import annotations

from urbanlens.dashboard.models.badges.queryset import BadgeManager, BadgeQuerySet

CategoryQuerySet = BadgeQuerySet
CategoryManager = BadgeManager

__all__ = ["CategoryManager", "CategoryQuerySet"]
