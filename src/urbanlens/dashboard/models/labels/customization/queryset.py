"""LabelCustomization queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LabelCustomizationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for per-user label display overrides."""


class LabelCustomizationManager(abstract.DashboardManager.from_queryset(LabelCustomizationQuerySet)):
    """Manager for LabelCustomization records."""
