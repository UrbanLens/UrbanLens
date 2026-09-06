"""QuerySet and manager for EpaFacility."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class EpaFacilityQuerySet(abstract.DashboardQuerySet):
    """Query helpers for persisted EPA ECHO facility records."""


class EpaFacilityManager(abstract.DashboardManager.from_queryset(EpaFacilityQuerySet)):
    """Manager for EpaFacility."""
