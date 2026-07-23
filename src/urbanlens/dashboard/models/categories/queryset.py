"""CategoryQuerySet and CategoryManager are now backed by LabelQuerySet."""

from __future__ import annotations

from urbanlens.dashboard.models.labels.queryset import LabelManager, LabelQuerySet

CategoryQuerySet = LabelQuerySet
CategoryManager = LabelManager

__all__ = ["CategoryManager", "CategoryQuerySet"]
