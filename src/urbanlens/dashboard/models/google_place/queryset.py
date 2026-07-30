"""QuerySet and manager for GooglePlace."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class GooglePlaceQuerySet(abstract.DashboardQuerySet):
    """Query helpers for coordinate-keyed Google Place cache rows."""


class GooglePlaceManager(abstract.DashboardManager.from_queryset(GooglePlaceQuerySet)):
    """Manager for GooglePlace."""
