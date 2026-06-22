"""BadgeCustomization queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class BadgeCustomizationQuerySet(abstract.QuerySet):
    """QuerySet for per-user badge display overrides."""


class BadgeCustomizationManager(abstract.Manager.from_queryset(BadgeCustomizationQuerySet)):
    """Manager for BadgeCustomization records."""
