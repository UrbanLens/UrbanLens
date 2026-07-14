"""BadgeCustomization queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class BadgeCustomizationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for per-user badge display overrides."""


class BadgeCustomizationManager(abstract.DashboardManager.from_queryset(BadgeCustomizationQuerySet)):
    """Manager for BadgeCustomization records."""
